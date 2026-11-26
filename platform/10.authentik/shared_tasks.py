"""Authentik shared tasks for API operations

Token Hierarchy (mirrors Vault):
- AUTHENTIK_ROOT_TOKEN: Full admin access, creates apps and issues app tokens
- AUTHENTIK_APP_TOKEN: Per-service, limited to own SSO configuration

Storage in Vault:
- secret/platform/<env>/authentik/root_token: Admin API token
- secret/platform/<env>/<service>/sso_*: Per-service SSO config
"""

import functools
import shlex

from invoke import task
from invoke.exceptions import Exit
from rich.markup import escape
from common.common import check_service
from common.core.environ import get_env, service_domain
from common.security.store import vault_token
from common.console import header, success, error, warning, info


def fail_closed(func):
    """Turn a `False` return into exit code 1, so a caller of `invoke` sees the failure."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if result is False:
            raise Exit(code=1)
        return result

    return wrapper


REMOTE_TIMEOUT_SECONDS = 600


def build_remote_script(
    env_name: str,
    env_suffix: str,
    internal_domain: str,
    compose_root: str = "/etc/dokploy/compose",
) -> str:
    """Build the remote script that runs token-init for ONE environment.

    The compose checkout glob matches the production and the staging directory.
    The script picks the directory that is the working directory of this
    environment's running `platform-authentik-server${ENV_SUFFIX}` container.
    It runs compose in the project of that container, because the `secrets`
    volume that token-init mounts belongs to that project, and it starts no
    dependency, so the running server and worker stay as they are. It sets
    `ENV_SUFFIX` in the shell env of compose, because Compose interpolates it into
    the readiness host `platform-authentik-server${ENV_SUFFIX}` of the token-init script.
    Exit codes: 11 container missing, 12 directory match is not exactly one,
    13 container has no compose project label.
    """
    q = shlex.quote
    vault_url = f"https://vault.{internal_domain}"
    return f"""set -eu
ENV_NAME={q(env_name)}
ENV_SUFFIX={q(env_suffix)}
SERVER="platform-authentik-server$ENV_SUFFIX"
WD=$(docker inspect -f '{{{{index .Config.Labels "com.docker.compose.project.working_dir"}}}}' "$SERVER" 2>/dev/null || true)
if [ -z "$WD" ]; then
  echo "FAIL: container $SERVER not found for environment $ENV_NAME" >&2
  exit 11
fi
PROJECT=$(docker inspect -f '{{{{index .Config.Labels "com.docker.compose.project"}}}}' "$SERVER" 2>/dev/null || true)
if [ -z "$PROJECT" ]; then
  echo "FAIL: container $SERVER has no compose project label" >&2
  exit 13
fi
WDR=$(cd "$WD" 2>/dev/null && pwd -P || true)
MATCHES=0
CHOSEN=""
for d in {compose_root}/platform-authentik-*/code/platform/10.authentik; do
  [ -d "$d" ] || continue
  DR=$(cd "$d" && pwd -P)
  if [ -n "$WDR" ] && [ "$DR" = "$WDR" ]; then
    MATCHES=$((MATCHES + 1))
    CHOSEN="$d"
  fi
done
if [ "$MATCHES" -ne 1 ]; then
  echo "FAIL: $MATCHES compose directories match $SERVER (working dir $WD); expected 1" >&2
  exit 12
fi
cd "$CHOSEN"
TMPENV=$(mktemp)
trap 'rm -f "$TMPENV"' EXIT
printf 'VAULT_INIT_TOKEN=%s\\n' "$VAULT_INIT_TOKEN" > "$TMPENV"
ENV={q(env_name)} ENV_SUFFIX={q(env_suffix)} VAULT_ADDR={q(vault_url)} \
  docker compose --env-file "$TMPENV" -p "$PROJECT" run --rm --no-deps \
  token-init </dev/null
"""


def _run_remote_script(
    host: str, script: str, token: str, user: str | None = None
) -> tuple[int, str, str]:
    """Run `script` on the VPS over non-interactive SSH with a bounded run time.

    The token goes to the remote shell on stdin, never in the process list.
    `user` defaults to the SSH user of the watchdog credentials.
    """
    from common.deploy.vps_ssh import ssh_script

    _, default_user, key_path, port = _ssh_credentials()
    user = user or default_user
    payload = f"export VAULT_INIT_TOKEN={shlex.quote(token)}\n{script}"
    return ssh_script(
        payload, host, user, key_path, port, timeout=REMOTE_TIMEOUT_SECONDS
    )


def _ssh_credentials():
    from common.deploy.vps_ssh import resolve_ssh_credentials

    return resolve_ssh_credentials()


@task
def status(c):
    """Check Authentik status"""
    return check_service(c, "authentik", "ak healthcheck")


SSH_FAILURES = {
    124: "SSH run time limit reached",
    255: "SSH failed: check the host key, the key file and the network",
}


def _failure_reason(rc: int, stderr: str) -> str:
    """Keep only the `FAIL:` lines of the remote script, else a fixed SSH reason.

    Container output can hold a token, so no other remote text reaches the terminal.
    """
    lines = [x.strip() for x in (stderr or "").splitlines() if x.startswith("FAIL:")]
    return escape("; ".join(lines)[-500:] or SSH_FAILURES.get(rc, ""))


def _vault_holds_root_token(env_name: str) -> bool:
    """True when Vault holds `root_token` or `api_token` for this environment."""
    from common.security.store import VaultSecrets, get_secrets

    secrets = get_secrets("platform", "authentik", env_name)
    try:
        return bool(secrets.get("root_token") or secrets.get("api_token"))
    except VaultSecrets.VaultSecretNotFoundError:
        return False


@task(help={"force": "Run token-init even when Vault already holds a token"})
@fail_closed
def create_root_token(c, force=False):
    """Create Authentik Root Token for SSO administration

    This creates the admin API token stored as 'root_token' in Vault.
    Requires VAULT_TOKEN (the deploy credential) to write to Vault.
    It skips the run when Vault already holds the token; use --force to run it again.

    The Authentik Root Token is used to:
    - Create SSO applications
    - Issue per-service app tokens
    - Manage providers and policies

    Example:
        export VAULT_TOKEN=<deploy or break-glass token>
        invoke authentik.shared.create-root-token
    """
    from common.security.store import VaultSecrets, get_secrets

    header("Creating Authentik Root Token", "SSO Admin Setup")

    e = get_env()
    env_name = e.get("ENV", "production")
    # The suffix follows the environment name, never a stale exported ENV_SUFFIX.
    env_suffix = get_env(env_name).get("ENV_SUFFIX") or ""
    if (e.get("ENV_SUFFIX") or "") != env_suffix:
        error(
            f"ENV_SUFFIX {e.get('ENV_SUFFIX')!r} does not match environment {env_name}",
            f"expected {env_suffix!r}; unset ENV_SUFFIX",
        )
        return False

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

    if not force:
        try:
            held = _vault_holds_root_token(env_name)
        except VaultSecrets.VaultError as exc:
            error("Cannot read Authentik secrets from Vault", escape(str(exc).strip()))
            return False
        if held:
            success(f"Authentik Root Token already in Vault for {env_name}; skipping")
            return True

    internal_domain = e.get("INTERNAL_DOMAIN")
    vps_host = e.get("VPS_HOST")
    if not internal_domain or not vps_host:
        error("INTERNAL_DOMAIN and VPS_HOST are required")
        return False

    info("Running token creation on server...")
    script = build_remote_script(env_name, env_suffix, internal_domain)
    rc, _out, err = _run_remote_script(
        vps_host, script, vault_root_token, user=e.get("VPS_SSH_USER")
    )
    if rc != 0:
        error(f"Failed to create token (exit {rc})", _failure_reason(rc, err) or None)
        return False

    # Verify in Vault
    authentik_secrets = get_secrets("platform", "authentik", env_name)
    root_token = authentik_secrets.get("root_token") or authentik_secrets.get(
        "api_token"
    )

    if root_token:
        success("Authentik Root Token created and stored in Vault")
        info(f"Vault path: secret/platform/{env_name}/authentik (key: root_token)")
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
) -> bool:
    """Bind every group policy. False when one binding fails: the app has no group gate.

    A binding that exists counts as bound, so a second run of the setup passes.
    """
    info("Binding access policies to application...")
    bound = True
    for group_name, policy_pk in policy_pks.items():
        found = client.get(
            f"{base_url}/api/v3/policies/bindings/?target={app_pk}&policy={policy_pk}"
        )
        if found.status_code == 200 and found.json().get("results"):
            info(f"Policy binding already exists: {group_name}")
            continue
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
        text = resp.text.lower()
        if resp.status_code == 201:
            success(f"Bound policy: {group_name} → application")
        elif resp.status_code == 400 and (
            "already exists" in text or "unique set" in text
        ):
            info(f"Policy binding already exists: {group_name}")
        else:
            error(f"Failed to bind policy {group_name}: {resp.status_code}")
            bound = False
    return bound


def _configure_embedded_outpost(client, base_url: str, provider_id: str) -> bool:
    """Add the provider to the embedded outpost. False when forward auth cannot work."""
    info("Configuring embedded outpost...")
    resp = client.get(
        f"{base_url}/api/v3/outposts/instances/?managed=goauthentik.io/outposts/embedded"
    )
    if resp.status_code != 200:
        error(f"Failed to read the embedded outpost: {resp.status_code}")
        return False
    results = resp.json()["results"]
    if not results:
        error("Embedded outpost not found - forward auth cannot work")
        return False
    outpost = results[0]
    current_providers = outpost.get("providers", [])
    if provider_id in current_providers:
        info("Provider already in outpost")
        return True
    current_providers.append(provider_id)
    resp = client.patch(
        f"{base_url}/api/v3/outposts/instances/{outpost['pk']}/",
        json={
            "providers": current_providers,
            "config": {"authentik_host": base_url},
        },
    )
    if resp.status_code != 200:
        error(f"Failed to update outpost: {resp.status_code}")
        return False
    success("Added provider to embedded outpost")
    return True


@task
@fail_closed
def create_proxy_app(
    c, name, slug, external_host, internal_host, port=None, allowed_groups="admins"
):
    """Create SSO application with proxy provider and access policy."""
    import httpx
    from common.security.store import get_secrets

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

        if not _bind_policies_to_app(client, base_url, app_pk, policy_pks):
            return False
        if not _configure_embedded_outpost(client, base_url, provider_id):
            return False

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
@fail_closed
def list_apps(c):
    """List all Authentik applications"""
    import httpx
    from common.security.store import get_secrets

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
@fail_closed
def setup_admin_group(c):
    """Ensure akadmin user is in admins group

    Creates 'admins' group if it doesn't exist and adds akadmin to it.
    This should be run once after Authentik deployment.

    Example:
        invoke authentik.shared.setup-admin-group
    """
    import httpx
    from common.security.store import get_secrets

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
