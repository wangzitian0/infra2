"""Authentik shared tasks for API operations

Token Hierarchy (mirrors Vault):
- AUTHENTIK_ROOT_TOKEN: Full admin access, creates apps and issues app tokens
- AUTHENTIK_APP_TOKEN: Per-service, limited to own SSO configuration

Storage in Vault:
- secret/platform/<env>/authentik/root_token: Admin API token
- secret/platform/<env>/<service>/sso_*: Per-service SSO config
"""

from invoke import task
from libs.common import check_service, get_env, service_domain
from libs.env import vault_token
from libs.console import header, success, error, warning, info


@task
def status(c):
    """Check Authentik status"""
    return check_service(c, "authentik", "ak healthcheck")


@task
def create_root_token(c):
    """Create Authentik Root Token for SSO administration

    This creates the admin API token stored as 'root_token' in Vault.
    Requires VAULT_TOKEN (the deploy credential) to write to Vault.

    The Authentik Root Token is used to:
    - Create SSO applications
    - Issue per-service app tokens
    - Manage providers and policies

    Example:
        export VAULT_TOKEN=<deploy or break-glass token>
        invoke authentik.shared.create-root-token
    """
    from libs.env import get_secrets

    header("Creating Authentik Root Token", "SSO Admin Setup")

    e = get_env()
    env_name = e.get("ENV", "production")
    vault_root_token = vault_token()

    if not vault_root_token:
        error("VAULT_TOKEN not set")
        info("Vault admin token needed to store Authentik root token")
        info(
            "The iac-runner passes its AppRole token; locally export a break-glass token "
            "(or /Token; item: bootstrap/vault/Root Token)"
        )
        info("as VAULT_TOKEN (bootstrap/05.vault README)")
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


def _verify_authentik_auth(client, base_url: str) -> bool:
    info("Verifying Authentik Root Token...")
    resp = client.get(f"{base_url}/api/v3/core/users/me/")
    if resp.status_code != 200:
        error(f"Root token auth failed: {resp.status_code}")
        return False
    user_data = resp.json()
    user = user_data.get("user", user_data)
    success(f"Authenticated as: {user['username']}")
    return True


def _ensure_authentik_groups(
    client, base_url: str, group_list: list[str]
) -> list[str] | None:
    info(f"Checking groups: {', '.join(group_list)}...")
    group_pks = []
    for group_name in group_list:
        resp = client.get(f"{base_url}/api/v3/core/groups/?name={group_name}")
        if resp.status_code != 200:
            error(f"Failed to query groups: {resp.status_code}")
            return None

        results = resp.json()["results"]
        if not results:
            warning(f"Group '{group_name}' not found, creating...")
            resp = client.post(
                f"{base_url}/api/v3/core/groups/", json={"name": group_name}
            )
            if resp.status_code != 201:
                error(f"Failed to create group: {resp.status_code}")
                return None
            group_pks.append(resp.json()["pk"])
            success(f"Created group: {group_name}")
        else:
            group_pks.append(results[0]["pk"])
            info(f"Found group: {group_name}")
    return group_pks


def _get_authentik_flows(client, base_url: str) -> tuple[str, str] | None:
    resp = client.get(
        f"{base_url}/api/v3/flows/instances/?slug=default-provider-authorization-implicit-consent"
    )
    if resp.status_code != 200 or not resp.json()["results"]:
        error("Default authorization flow not found")
        return None
    auth_flow_uuid = resp.json()["results"][0]["pk"]

    resp = client.get(
        f"{base_url}/api/v3/flows/instances/?slug=default-provider-invalidation-flow"
    )
    if resp.status_code != 200 or not resp.json()["results"]:
        resp = client.get(
            f"{base_url}/api/v3/flows/instances/?slug=default-invalidation-flow"
        )
        if resp.status_code != 200 or not resp.json()["results"]:
            error("Invalidation flow not found")
            return None
    invalidation_flow_uuid = resp.json()["results"][0]["pk"]
    return auth_flow_uuid, invalidation_flow_uuid


def _ensure_access_policies(
    client, base_url: str, slug: str, group_list: list[str]
) -> dict[str, str] | None:
    info(f"Creating access policies (groups: {', '.join(group_list)})...")
    policy_pks = {}

    for group_name in group_list:
        policy_name = f"{slug}-require-{group_name}"
        resp = client.get(f"{base_url}/api/v3/policies/expression/?name={policy_name}")
        if resp.status_code == 200 and resp.json()["results"]:
            policy_pks[group_name] = resp.json()["results"][0]["pk"]
            info(f"Policy already exists: {policy_name}")
        else:
            safe_group_name = group_name.replace("'", "\\'")
            resp = client.post(
                f"{base_url}/api/v3/policies/expression/",
                json={
                    "name": policy_name,
                    "execution_logging": False,
                    "expression": f"return ak_is_group_member(request.user, name='{safe_group_name}')",
                },
            )
            if resp.status_code != 201:
                error(
                    f"Failed to create policy for group {group_name}: {resp.status_code} - {resp.text}"
                )
                return None
            policy_pks[group_name] = resp.json()["pk"]
            success(f"Created policy: require {group_name} membership")
    return policy_pks


def _ensure_proxy_provider(
    client,
    base_url: str,
    slug: str,
    external_host: str,
    internal_url: str,
    auth_flow_uuid: str,
    invalidation_flow_uuid: str,
) -> str | None:
    provider_name = f"{slug}-proxy"
    resp = client.get(f"{base_url}/api/v3/providers/proxy/")
    if resp.status_code == 200:
        for provider in resp.json()["results"]:
            if provider["name"] == provider_name:
                provider_id = provider["pk"]
                info(f"Provider already exists: {provider_name} (pk: {provider_id})")
                return provider_id

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
        return None
    provider_id = resp.json()["pk"]
    success(f"Created proxy provider: {provider_id}")
    return provider_id


def _ensure_application(
    client, base_url: str, name: str, slug: str, provider_id: str
) -> tuple[str, str] | None:
    resp = client.get(f"{base_url}/api/v3/core/applications/")
    if resp.status_code == 200:
        for app in resp.json()["results"]:
            if app["slug"] == slug:
                info(f"Application already exists: {name} (slug: {app['slug']})")
                return app["slug"], app["pk"]

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
        return None
    app_data = resp.json()
    success(f"Created application: {name} (slug: {app_data['slug']})")
    return app_data["slug"], app_data["pk"]


def _bind_policies_to_app(
    client, base_url: str, app_pk: str, policy_pks: dict[str, str]
) -> None:
    info("Binding access policies to application...")
    for group_name, policy_pk in policy_pks.items():
        resp = client.post(
            f"{base_url}/api/v3/policies/bindings/",
            json={
                "policy": policy_pk,
                "target": app_pk,
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


def _configure_embedded_outpost(client, base_url: str, provider_id: str) -> None:
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


@task
def create_proxy_app(
    c, name, slug, external_host, internal_host, port=None, allowed_groups="admins"
):
    """Create SSO application with proxy provider and access policy."""
    import httpx
    from libs.env import get_secrets

    header(f"Creating SSO App: {name}", "Proxy Provider + Access Policy")

    e = get_env()
    env_name = e.get("ENV", "production")

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

    client = httpx.Client(
        verify=True, headers={"Authorization": f"Bearer {root_token}"}
    )
    try:
        if not _verify_authentik_auth(client, base_url):
            return False

        if _ensure_authentik_groups(client, base_url, group_list) is None:
            return False

        flows = _get_authentik_flows(client, base_url)
        if not flows:
            return False
        auth_flow_uuid, invalidation_flow_uuid = flows

        policy_pks = _ensure_access_policies(client, base_url, slug, group_list)
        if policy_pks is None:
            return False

        provider_id = _ensure_proxy_provider(
            client,
            base_url,
            slug,
            external_host,
            internal_url,
            auth_flow_uuid,
            invalidation_flow_uuid,
        )
        if not provider_id:
            return False

        app_info = _ensure_application(client, base_url, name, slug, provider_id)
        if not app_info:
            return False
        app_slug, app_pk = app_info

        _bind_policies_to_app(client, base_url, app_pk, policy_pks)
        _configure_embedded_outpost(client, base_url, provider_id)

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
