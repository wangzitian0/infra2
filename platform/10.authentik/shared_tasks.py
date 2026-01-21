"""Authentik shared tasks for API operations

Token Hierarchy (mirrors Vault):
- AUTHENTIK_ROOT_TOKEN: Full admin access, creates apps and issues app tokens
- AUTHENTIK_APP_TOKEN: Per-service, limited to own SSO configuration

Storage in Vault:
- secret/platform/<env>/authentik/root_token: Admin API token
- secret/platform/<env>/<service>/sso_*: Per-service SSO config
"""

import os
from invoke import task
from libs.common import check_service, get_env, service_domain
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
    env_name = e.get("ENV", "production")
    vault_root_token = os.getenv("VAULT_ROOT_TOKEN")

    if not vault_root_token:
        error("VAULT_ROOT_TOKEN not set")
        info("Vault admin token needed to store Authentik root token")
        info(
            "Get from: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' "
            "(or /Token; item: bootstrap/vault/Root Token)"
        )
        info("Then: export VAULT_ROOT_TOKEN=<token>")
        return False

    info("Running token creation on server...")

    # Write token to temp file on remote to avoid exposing in process list
    script = f"""
set -e
cd /etc/dokploy/compose/platform-authentik-*/code/platform/10.authentik
# Create temp env file with token (more secure than command line args)
TMPENV=$(mktemp)
echo "VAULT_INIT_TOKEN=$VAULT_INIT_TOKEN" > "$TMPENV"
echo "VAULT_INIT_ADDR=https://vault.{e["INTERNAL_DOMAIN"]}" >> "$TMPENV"
docker compose run --rm --env-file "$TMPENV" token-init
rm -f "$TMPENV"
"""

    # Pass token via environment variable to SSH, not in command
    result = c.run(
        f"ssh root@{e['VPS_HOST']} 'VAULT_INIT_TOKEN=\"$VAULT_INIT_TOKEN\" bash -s'",
        env={"VAULT_INIT_TOKEN": vault_root_token},
        in_stream=script,
        warn=True,
    )

    if not result.ok:
        error("Failed to create token")
        return False

    # Verify in Vault
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get(
        "api_token"
    )

    if root_token:
        success("Authentik Root Token created and stored in Vault")
        info(f"Vault path: secret/platform/{env_name}/authentik (key: root_token)")
        info(f"Token prefix: {root_token[:20]}...")
        info("\nYou can now create SSO apps:")
        info(
            "  invoke authentik.shared.create-proxy-app --name=Portal --slug=portal ..."
        )
        return True
    else:
        warning("Token creation ran but not found in Vault")
        return False


@task
def create_proxy_app(
    c, name, slug, external_host, internal_host, port=None, allowed_groups="admins"
):
    """Create SSO application with proxy provider and access policy
    
    Uses Authentik Root Token to create a forward-auth protected application.
    By default, only users in 'admins' group can access.
    
    Args:
        name: Application name (e.g., "Portal")
        slug: Application slug (e.g., "portal")  
        external_host: External URL (e.g., "https://home.example.com")
        internal_host: Internal service (e.g., "platform-portal${ENV_SUFFIX}")
        port: Internal port (default: 8080)
        allowed_groups: Comma-separated group names (default: "admins")
    
    Example:
        # Only admins can access
        invoke authentik.shared.create-proxy-app \\
            --name="Portal" \\
            --slug="portal" \\
            --external-host="https://home.example.com" \\
            --internal-host="platform-portal${ENV_SUFFIX}"
        
        # Multiple groups
        invoke authentik.shared.create-proxy-app \\
            --name="App" --slug="app" \\
            --external-host="https://app.example.com" \\
            --internal-host="app" \\
            --allowed-groups="admins,developers"
    """
    import httpx
    from libs.env import get_secrets

    header(f"Creating SSO App: {name}", "Proxy Provider + Access Policy")

    e = get_env()
    env_name = e.get("ENV", "production")

    # Get Authentik Root Token from Vault
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get(
        "api_token"
    )

    if not root_token:
        error("Authentik Root Token not found in Vault")
        info("\nRun first: invoke authentik.shared.create-root-token")
        return False

    base_host = service_domain("sso", e)
    if not base_host:
        error("INTERNAL_DOMAIN not set")
        return False
    base_url = f"https://{base_host}"
    port = port or 8080
    internal_url = f"http://{internal_host}:{port}"
    group_list = [g.strip() for g in allowed_groups.split(",")]

    try:
        client = httpx.Client(
            verify=True, headers={"Authorization": f"Bearer {root_token}"}
        )

        # Test auth
        info("Verifying Authentik Root Token...")
        resp = client.get(f"{base_url}/api/v3/core/users/me/")
        if resp.status_code != 200:
            error(f"Root token auth failed: {resp.status_code}")
            return False

        user_data = resp.json()
        # Handle nested user object
        user = user_data.get("user", user_data)
        success(f"Authenticated as: {user['username']}")

        # Ensure groups exist
        info(f"Checking groups: {', '.join(group_list)}...")
        group_pks = []
        for group_name in group_list:
            resp = client.get(f"{base_url}/api/v3/core/groups/?name={group_name}")
            if resp.status_code != 200:
                error(f"Failed to query groups: {resp.status_code}")
                return False

            results = resp.json()["results"]
            if not results:
                warning(f"Group '{group_name}' not found, creating...")
                resp = client.post(
                    f"{base_url}/api/v3/core/groups/", json={"name": group_name}
                )
                if resp.status_code != 201:
                    error(f"Failed to create group: {resp.status_code}")
                    return False
                group_pks.append(resp.json()["pk"])
                success(f"Created group: {group_name}")
            else:
                group_pks.append(results[0]["pk"])
                info(f"Found group: {group_name}")

        # Get auth flow UUID
        resp = client.get(
            f"{base_url}/api/v3/flows/instances/?slug=default-provider-authorization-implicit-consent"
        )
        if resp.status_code != 200 or not resp.json()["results"]:
            error("Default authorization flow not found")
            return False

        auth_flow_uuid = resp.json()["results"][0]["pk"]

        # Get invalidation flow UUID
        resp = client.get(
            f"{base_url}/api/v3/flows/instances/?slug=default-provider-invalidation-flow"
        )
        if resp.status_code != 200 or not resp.json()["results"]:
            # Try alternative flow name
            resp = client.get(
                f"{base_url}/api/v3/flows/instances/?slug=default-invalidation-flow"
            )
            if resp.status_code != 200 or not resp.json()["results"]:
                error("Invalidation flow not found")
                return False

        invalidation_flow_uuid = resp.json()["results"][0]["pk"]

        # Create group-based access policies and collect PKs for binding
        info(f"Creating access policies (groups: {', '.join(group_list)})...")
        policy_pks = {}  # Store policy PKs for later binding

        for group_name in group_list:
            policy_name = f"{slug}-require-{group_name}"

            # Check if policy already exists
            resp = client.get(
                f"{base_url}/api/v3/policies/expression/?name={policy_name}"
            )
            if resp.status_code == 200 and resp.json()["results"]:
                policy_pks[group_name] = resp.json()["results"][0]["pk"]
                info(f"Policy already exists: {policy_name}")
            else:
                resp = client.post(
                    f"{base_url}/api/v3/policies/expression/",
                    json={
                        "name": policy_name,
                        "execution_logging": False,
                        "expression": f"return ak_is_group_member(request.user, name='{group_name}')",
                    },
                )

                if resp.status_code != 201:
                    error(
                        f"Failed to create policy for group {group_name}: {resp.status_code} - {resp.text}"
                    )
                    return False

                policy_pks[group_name] = resp.json()["pk"]
                success(f"Created policy: require {group_name} membership")

        # Check if provider already exists
        provider_name = f"{slug}-proxy"
        resp = client.get(f"{base_url}/api/v3/providers/proxy/?name={provider_name}")
        if resp.status_code == 200 and resp.json()["results"]:
            provider_id = resp.json()["results"][0]["pk"]
            info(f"Provider already exists: {provider_name} (pk: {provider_id})")
        else:
            # Create proxy provider
            info(f"Creating proxy provider for {external_host}...")
            resp = client.post(
                f"{base_url}/api/v3/providers/proxy/",
                json={
                    "name": provider_name,
                    "authorization_flow": auth_flow_uuid,
                    "invalidation_flow": invalidation_flow_uuid,
                    "mode": "forward_single",
                    "external_host": external_host,
                    "internal_host": internal_url,
                },
            )

            if resp.status_code != 201:
                error(f"Failed to create provider: {resp.status_code} - {resp.text}")
                return False

            provider_id = resp.json()["pk"]
            success(f"Created proxy provider: {provider_id}")

        # Check if application already exists
        resp = client.get(f"{base_url}/api/v3/core/applications/?slug={slug}")
        if resp.status_code == 200 and resp.json()["results"]:
            app_data = resp.json()["results"][0]
            app_slug = app_data["slug"]
            app_pk = app_data["pk"]
            info(f"Application already exists: {name} (slug: {app_slug})")
        else:
            # Create application
            info(f"Creating application: {name}...")
            resp = client.post(
                f"{base_url}/api/v3/core/applications/",
                json={
                    "name": name,
                    "slug": slug,
                    "provider": provider_id,
                },
            )

            if resp.status_code != 201:
                error(f"Failed to create application: {resp.status_code} - {resp.text}")
                return False

            app_data = resp.json()
            app_slug = app_data["slug"]
            app_pk = app_data["pk"]
            success(f"Created application: {name} (slug: {app_slug})")

        # Bind policies to APPLICATION (not provider) - use app_pk UUID
        info("Binding access policies to application...")
        for group_name, policy_pk in policy_pks.items():
            resp = client.post(
                f"{base_url}/api/v3/policies/bindings/",
                json={
                    "policy": policy_pk,
                    "target": app_pk,  # Application UUID, not provider ID
                    "enabled": True,
                    "order": 0,
                    "timeout": 30,
                },
            )

            if resp.status_code == 201:
                success(f"Bound policy: {group_name} → application")
            elif resp.status_code == 400 and "already exists" in resp.text.lower():
                info(f"Policy binding already exists: {group_name}")
            else:
                warning(f"Failed to bind policy {group_name}: {resp.status_code}")

        # Add provider to embedded outpost
        info("Configuring embedded outpost...")
        resp = client.get(
            f"{base_url}/api/v3/outposts/instances/?managed=goauthentik.io/outposts/embedded"
        )
        if resp.status_code == 200 and resp.json()["results"]:
            outpost = resp.json()["results"][0]
            outpost_pk = outpost["pk"]
            current_providers = outpost.get("providers", [])

            if provider_id not in current_providers:
                current_providers.append(provider_id)

                # Update outpost with provider and authentik_host
                resp = client.patch(
                    f"{base_url}/api/v3/outposts/instances/{outpost_pk}/",
                    json={
                        "providers": current_providers,
                        "config": {"authentik_host": base_url},
                    },
                )

                if resp.status_code == 200:
                    success("Added provider to embedded outpost")
                else:
                    warning(f"Failed to update outpost: {resp.status_code}")
            else:
                info("Provider already in outpost")
        else:
            warning("Embedded outpost not found - forward auth may not work")

        info(f"\n✨ SSO protection enabled for {external_host}")
        info(f"Access control: Only users in [{', '.join(group_list)}] can access")
        info(f"Admin URL: {base_url}/if/admin/#/core/applications/{app_slug}")

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

    header("Listing SSO Applications", "")

    e = get_env()
    env_name = e.get("ENV", "production")

    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get(
        "api_token"
    )

    if not root_token:
        error("Authentik Root Token not found")
        return False

    base_host = service_domain("sso", e)
    if not base_host:
        error("INTERNAL_DOMAIN not set")
        return False
    base_url = f"https://{base_host}"

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


@task
def setup_admin_group(c):
    """Ensure akadmin user is in admins group

    Creates 'admins' group if it doesn't exist and adds akadmin to it.
    This should be run once after Authentik deployment.

    Example:
        invoke authentik.shared.setup-admin-group
    """
    import httpx
    from libs.env import get_secrets

    header("Setting up Admin Group", "Access Control")

    e = get_env()
    env_name = e.get("ENV", "production")

    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get(
        "api_token"
    )

    if not root_token:
        error("Authentik Root Token not found")
        info("Run first: invoke authentik.shared.create-root-token")
        return False

    base_host = service_domain("sso", e)
    if not base_host:
        error("INTERNAL_DOMAIN not set")
        return False
    base_url = f"https://{base_host}"

    try:
        client = httpx.Client(headers={"Authorization": f"Bearer {root_token}"})

        # Find akadmin user
        info("Finding akadmin user...")
        resp = client.get(f"{base_url}/api/v3/core/users/?username=akadmin")
        if resp.status_code != 200 or not resp.json()["results"]:
            error("akadmin user not found")
            return False

        user_pk = resp.json()["results"][0]["pk"]
        success(f"Found akadmin user: {user_pk}")

        # Check/create admins group
        info("Checking for 'admins' group...")
        resp = client.get(f"{base_url}/api/v3/core/groups/?name=admins")

        if resp.status_code != 200:
            error(f"Failed to query groups: {resp.status_code}")
            return False

        results = resp.json()["results"]
        if not results:
            info("Creating 'admins' group...")
            resp = client.post(
                f"{base_url}/api/v3/core/groups/",
                json={
                    "name": "admins",
                    "is_superuser": False,
                },
            )
            if resp.status_code != 201:
                error(f"Failed to create group: {resp.status_code}")
                return False
            group_pk = resp.json()["pk"]
            success(f"Created 'admins' group: {group_pk}")
        else:
            group_pk = results[0]["pk"]
            success(f"Found 'admins' group: {group_pk}")

        # Add user to group
        info("Adding akadmin to admins group...")
        resp = client.post(
            f"{base_url}/api/v3/core/groups/{group_pk}/add_user/", json={"pk": user_pk}
        )

        if resp.status_code == 204:
            success("akadmin added to admins group")
        elif resp.status_code == 400:
            info("akadmin already in admins group")
        else:
            error(f"Failed to add user to group: {resp.status_code}")
            return False

        info("\n✨ Admin group configured")
        info("Users in 'admins' group can now access admin-protected applications")

        return True

    except Exception as exc:
        error(f"API error: {exc}")
        return False
    finally:
        client.close()
