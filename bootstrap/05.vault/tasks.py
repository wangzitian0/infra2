"""
Vault deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
"""

import json
import os

from invoke import task
from libs.deployer import Deployer
from libs.common import get_env
from libs.console import (
    header,
    success,
    error,
    warning,
    info,
    prompt_action,
    run_with_status,
)
from libs.vault_tokens import (
    TOKEN_PERIOD,
    VaultTokenTarget,
    accessor_kv_path,
    display_name as vault_token_display_name,
    normalize_selector,
    policy_name as vault_policy_name,
    token_for_output,
)
from typing import Any


class VaultDeployer(Deployer):
    """Vault deployer using libs/ system"""

    service = "vault"
    project = "bootstrap"
    compose_path = "bootstrap/05.vault/compose.yaml"
    data_path = "/data/bootstrap/vault"
    uid = "100"  # Vault official image runs as uid 100
    gid = "1000"
    chmod = "755"

    # Domain configuration via Dokploy API
    subdomain = "vault"
    service_port = 8200
    service_name = "vault"

    @classmethod
    def pre_compose(cls, c) -> dict | None:
        """Prepare data directory, upload config, and fetch secrets."""
        # 1. Prepare directories
        if not cls._prepare_dirs(c):
            return None

        e = cls.env()
        ssh_user = e.get("VPS_SSH_USER") or "root"
        header("Vault pre_compose", "Preparing resources")

        # 2. Upload config
        if not cls.upload_config(c):
            return None

        # 3. Create subdirectories with strict permissions
        # vault user (100) needs write access to file/ logs/
        run_with_status(
            c,
            f"ssh {ssh_user}@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/{{file,logs,config}} && chown -R {cls.uid}:{cls.gid} {cls.data_path}'",
            "Set directory structure and permissions",
        )

        # 4. Fetch 1Password Secrets for Unsealer
        info("Fetching secrets from 1Password...")
        env_vars = {
            "INTERNAL_DOMAIN": e.get("INTERNAL_DOMAIN"),
        }

        try:
            from libs.env import OpSecrets

            vault_result = c.run(
                "op vault get Infra2 --format json", hide=True, warn=True
            )
            if vault_result.ok:
                vault = json.loads(vault_result.stdout)
                env_vars["OP_VAULT_ID"] = vault["id"]
            else:
                error("Failed to resolve Infra2 vault ID for 1Password Connect")
                return None

            # OP_CONNECT_TOKEN (from 1Password Connect service account)
            # Item: "bootstrap/1password/VPS-01 Access Token: own_service"
            token_item = OpSecrets(
                item="bootstrap/1password/VPS-01 Access Token: own_service"
            )
            token = token_item.get("credential")
            if token:
                env_vars["OP_CONNECT_TOKEN"] = token
            else:
                warning("Could not finding OP_CONNECT_TOKEN in 1Password")

            # OP_ITEM_ID (Item "bootstrap/vault/Unseal Keys" where unseal keys are stored)
            try:
                # We need the Item ID, not content. Use CLI wrapper or name
                # If item doesn't exist yet (pre-init), we might skip or leave empty.
                # Here we assume it might exist or will be created.
                # passing name might work if unsealer supports it, but compose expects ID usually.
                # Let's try to look it up.
                cmd = "op item get 'bootstrap/vault/Unseal Keys' --vault Infra2 --format json"
                res = c.run(cmd, hide=True, warn=True)
                if res.ok:
                    item = json.loads(res.stdout)
                    env_vars["OP_ITEM_ID"] = item["id"]
                else:
                    info(
                        "Vault item 'bootstrap/vault/Unseal Keys' not found (normal if first run)"
                    )
                    env_vars["OP_ITEM_ID"] = ""
            except Exception as ex:
                warning(f"Failed to lookup Vault item ID: {ex}")
                env_vars["OP_ITEM_ID"] = ""

        except ImportError:
            error("Missing libs.env dependencies")
            return None
        except Exception as ex:
            error(f"Failed to fetch secrets: {ex}")
            return None

        success("pre_compose complete")
        return env_vars

    @classmethod
    def upload_config(cls, c) -> bool:
        """Upload Vault config file."""
        e = cls.env()
        ssh_user = e.get("VPS_SSH_USER") or "root"
        # Ensure config dir exists first
        c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/config'")

        result = run_with_status(
            c,
            f"scp bootstrap/05.vault/vault.hcl {ssh_user}@{e['VPS_HOST']}:{cls.data_path}/config/",
            "Upload config file",
        )
        return result.ok

    @classmethod
    def composing(cls, c, env_vars: dict) -> str:
        """Deploy Vault via Dokploy API (using GitHub provider)"""
        from libs.dokploy import ensure_project, get_dokploy
        from libs.const import GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH

        e = cls.env()
        header(f"{cls.service} composing", "Deploying via Dokploy API")

        # Ensure project exists
        domain = e.get("INTERNAL_DOMAIN")
        host = f"cloud.{domain}" if domain else None

        # Priority: Hardcoded "bootstrap" for this module
        project_name = cls.project

        project_id, env_id = ensure_project(
            project_name, f"Bootstrap services: {project_name}", host=host
        )
        if not env_id:
            from invoke.exceptions import Exit

            error("Failed to get environment ID")
            raise Exit("Failed to get environment ID", code=1)

        # Deploy compose using GitHub provider
        client = get_dokploy(host=host)

        # Get GitHub provider ID
        github_id = client.get_github_provider_id()
        if not github_id:
            from invoke.exceptions import Exit

            error(
                "No GitHub provider configured in Dokploy. Please add one in Settings -> Git Providers."
            )
            raise Exit("No GitHub provider found", code=1)

        info(f"Using GitHub provider: {github_id}")

        # Check if exists
        existing = client.find_compose_by_name(cls.service, project_name)

        if existing:
            compose_id = existing["composeId"]
            info("Updating existing compose service")
            client.update_compose(
                compose_id,
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
            )
        else:
            info("Creating new compose service with GitHub provider")
            result = client.create_compose(
                environment_id=env_id,
                name=cls.service,
                app_name=f"bootstrap-{cls.service}",
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
            )
            compose_id = result["composeId"]

        # Update environment variables
        info("Updating environment variables (from libs)")
        # Filter out internal keys or empty values
        env_content = "\n".join(
            [f"{k}={v}" for k, v in env_vars.items() if v is not None]
        )
        client.update_compose(compose_id, env=env_content)

        info(f"Deploying compose {compose_id}...")
        client.deploy_compose(compose_id)

        # Configure domain via Dokploy API (using ensure_domains for idempotency)
        if cls.subdomain and cls.service_port:
            domain_host = f"{cls.subdomain}.{domain}"
            info(f"Ensuring domain: {domain_host}")

            desired_domains = [
                {"host": domain_host, "port": cls.service_port, "https": True}
            ]
            result = client.ensure_domains(
                compose_id=compose_id,
                desired_domains=desired_domains,
                service_name=cls.service_name,
            )

            if result["created"] > 0:
                success(f"Domain configured: https://{domain_host}")
                client.deploy_compose(compose_id)
            elif result["skipped"] > 0:
                info(f"Domain already configured: {domain_host}")

        success(f"Deployed {cls.service} (composeId: {compose_id})")
        return compose_id

    @classmethod
    def post_compose(cls, c, shared_tasks: Any) -> bool:
        """Verify deployment"""
        header("Vault post_compose", "Verifying")
        if cls.check_status(c, shared_tasks):
            success("Vault is reachable")
            return True
        warning("Vault may need initialization")
        return False

    @classmethod
    def check_status(cls, c, shared_tasks: Any) -> bool:
        """Custom status check for Vault"""
        e = cls.env()
        result = c.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health",
            warn=True,
            hide=True,
        )
        if not result.ok:
            error(
                "Vault status check failed: curl command did not complete successfully."
            )
            stderr = getattr(result, "stderr", "") or ""
            if stderr.strip():
                error(stderr.strip())
            return False
        status_code = (result.stdout or "").strip()
        if not status_code:
            error("Vault status check failed: no HTTP status code returned by curl.")
            return False
        if status_code in {"200", "429", "472", "473"}:
            return True
        warning(f"Vault health endpoint returned unexpected status code: {status_code}")
        return False

    @classmethod
    def is_reachable(cls, c) -> bool:
        """Check if Vault is reachable (any valid response from health endpoint)"""
        e = cls.env()
        result = c.run(
            f"curl -s -o /dev/null -w '%{{http_code}}' https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health",
            warn=True,
            hide=True,
        )
        if not result.ok:
            return False
        status_code = (result.stdout or "").strip()
        # 501=not initialized, 503=sealed - both mean Vault is reachable
        return status_code in {"200", "429", "472", "473", "501", "503"}


# Standard tasks
# We don't use make_tasks fully because Vault requires extra steps (init, unseal)
prepare = task(lambda c: VaultDeployer.pre_compose(c), name="prepare")
upload_config = task(lambda c: VaultDeployer.upload_config(c), name="upload-config")


@task(name="deploy")
def deploy(c):
    """Deploy Vault (prepares, injects vars, and composes)"""
    # Fetch env vars (includes secrets and domain)
    env_vars = VaultDeployer.pre_compose(c)
    if env_vars is None:
        error("Deployment failed: pre_compose returned No config")
        from invoke.exceptions import Exit

        raise Exit("pre_compose failed", code=1)

    VaultDeployer.composing(c, env_vars)


@task(pre=[deploy])
def init(c):
    """Initialize Vault (checks reachability first)"""
    e = get_env()
    header("Vault init", "Checking reachability")

    # Pre-check: Vault must be reachable before init (501/503 are OK - means service is up)
    if not VaultDeployer.is_reachable(c):
        error("Vault is not reachable. Deployment may have failed.")
        info("Check logs: ssh root@<host> 'docker logs vault'")
        from invoke.exceptions import Exit

        raise Exit("Vault not reachable", code=1)

    success("Vault is reachable, ready for initialization")
    print(f"export VAULT_ADDR=https://vault.{e['INTERNAL_DOMAIN']}")
    print("vault operator init")
    prompt_action(
        "Initialize Vault", ["Run the commands above", "Save keys to 1Password"]
    )


@task
def unseal(c):
    """(Manual trigger) Restart unsealer container"""
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    header("Vault unseal", "Triggering unsealer")
    c.run(
        f"ssh {ssh_user}@{e['VPS_HOST']} 'docker logs --tail 20 vault-unsealer'",
        warn=True,
    )
    c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'docker restart vault-unsealer'")
    success("Unsealer restarted")


@task
def status(c):
    """Check Vault status"""
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    header("Vault status", "Checking")
    c.run(f"curl -s https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health", warn=True)
    c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'docker ps | grep vault'", warn=True)


def _vault_token_targets(root_dir: str) -> list[VaultTokenTarget]:
    """Return all services that should receive a Vault app token."""
    projects = [
        (
            "bootstrap",
            os.path.join(root_dir, "bootstrap"),
            {
                "iac_runner": "06.iac_runner",
            },
            "bootstrap",
        ),
        (
            "platform",
            os.path.join(root_dir, "platform"),
            {
                "postgres": "01.postgres",
                "redis": "02.redis",
                "minio": "03.minio",
                "authentik": "10.authentik",
                "alerting": "12.alerting",
                "activepieces": "22.activepieces",
                "prefect": "23.prefect",
            },
            "platform",
        ),
        (
            "finance_report",
            os.path.join(root_dir, "finance_report", "finance_report"),
            {
                "postgres": "01.postgres",
                "redis": "02.redis",
                "app": "10.app",
            },
            "finance_report",
        ),
    ]

    targets: list[VaultTokenTarget] = []
    for project_name, project_dir, service_map, dokploy_project in projects:
        for service, service_dir in service_map.items():
            targets.append(
                VaultTokenTarget(
                    project=project_name,
                    service=service,
                    service_dir=service_dir,
                    project_dir=project_dir,
                    dokploy_project=dokploy_project,
                )
            )
    return targets


def _select_token_targets(
    targets: list[VaultTokenTarget],
    project: str | None,
    service: str | None,
) -> list[VaultTokenTarget]:
    """Filter token targets for a targeted repair."""
    selected = []
    for target in targets:
        if project and target.project != project:
            continue
        if service and target.service != service:
            continue
        selected.append(target)
    return selected


def _vault_env(vault_addr: str, root_token: str) -> dict[str, str]:
    return {"VAULT_ADDR": vault_addr, "VAULT_TOKEN": root_token}


def _read_tracked_accessor(
    c,
    *,
    vault_addr: str,
    root_token: str,
    project: str,
    env_name: str,
    service: str,
) -> str | None:
    """Read the previously active accessor for a service token, if any."""
    path = accessor_kv_path(project, env_name, service)
    result = c.run(
        f"vault kv get -format=json {path}",
        env=_vault_env(vault_addr, root_token),
        hide=True,
        warn=True,
    )
    if not result.ok:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        warning(f"Could not parse tracked accessor metadata at {path}")
        return None

    accessor = data.get("data", {}).get("data", {}).get("accessor")
    return accessor if isinstance(accessor, str) and accessor else None


def _lookup_accessor_for_token(
    c,
    *,
    vault_addr: str,
    token: str | None,
) -> str | None:
    """Look up an app token's own accessor without exposing it as a CLI arg."""
    if not token:
        return None
    result = c.run(
        "vault token lookup -format=json",
        env={"VAULT_ADDR": vault_addr, "VAULT_TOKEN": token},
        hide=True,
        warn=True,
    )
    if not result.ok:
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    accessor = data.get("data", {}).get("accessor")
    return accessor if isinstance(accessor, str) and accessor else None


def _write_tracked_accessor(
    c,
    *,
    vault_addr: str,
    root_token: str,
    project: str,
    env_name: str,
    service: str,
    accessor: str,
    policy: str,
    display: str,
) -> bool:
    """Track the active accessor so later rotations can revoke the old token."""
    path = accessor_kv_path(project, env_name, service)
    result = c.run(
        " ".join(
            [
                "vault kv put",
                path,
                f"accessor={accessor}",
                f"policy={policy}",
                f"display_name={display}",
                f"period={TOKEN_PERIOD}",
            ]
        ),
        env=_vault_env(vault_addr, root_token),
        hide=True,
        warn=True,
    )
    return bool(result.ok)


def _revoke_accessor(
    c,
    *,
    vault_addr: str,
    root_token: str,
    accessor: str | None,
    new_accessor: str,
) -> bool:
    """Revoke the previous token accessor after the replacement is tracked."""
    if not accessor or accessor == new_accessor:
        return True

    result = c.run(
        f"vault token revoke -accessor {accessor}",
        env=_vault_env(vault_addr, root_token),
        hide=True,
        warn=True,
    )
    if not result.ok:
        warning(f"Could not revoke previous token accessor {accessor}")
    return bool(result.ok)


@task(
    help={
        "project": "Limit token setup to one project, e.g. finance_report.",
        "service": "Limit token setup to one service, e.g. app.",
        "deploy": "Update Dokploy env and trigger redeploy after token creation.",
        "revoke_old": "Revoke the previously tracked accessor after successful Dokploy update.",
    }
)
def setup_tokens(c, project=None, service=None, deploy=True, revoke_old=True):
    """Generate read-only tokens for platform and finance_report services"""
    import io

    header("Vault Token Setup", "Generating service tokens")

    # Check VAULT_ROOT_TOKEN
    root_token = os.getenv("VAULT_ROOT_TOKEN")
    if not root_token:
        error("VAULT_ROOT_TOKEN not set")
        info(
            "Get from: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' "
            "(or /Token; item: bootstrap/vault/Root Token)"
        )
        info("Then run: export VAULT_ROOT_TOKEN=<token>")
        return

    e = get_env()
    vault_addr = e.get("VAULT_ADDR", f"https://vault.{e['INTERNAL_DOMAIN']}")

    success(f"Using Vault: {vault_addr}")
    print("")

    current_dir = os.path.dirname(os.path.abspath(__file__))  # bootstrap/05.vault
    root_dir = os.path.dirname(os.path.dirname(current_dir))

    env_name = e.get("ENV", "production")
    target_project = normalize_selector(project, label="project")
    target_service = normalize_selector(service, label="service")
    strict_dokploy = bool(target_project or target_service)
    targets = _select_token_targets(
        _vault_token_targets(root_dir),
        target_project,
        target_service,
    )
    if not targets:
        error(
            "No Vault token targets matched",
            f"project={target_project or '*'} service={target_service or '*'}",
        )
        from invoke.exceptions import Exit

        raise Exit("No matching Vault token targets", code=1)

    info("Validating VAULT_ROOT_TOKEN before bulk operations...")
    validate_result = c.run(
        "vault token lookup -format=json",
        env={"VAULT_ADDR": vault_addr, "VAULT_TOKEN": root_token},
        hide=True,
        warn=True,
    )

    if not validate_result.ok:
        error("VAULT_ROOT_TOKEN is invalid or expired")
        error("Get new token: op read 'op://Infra2/.../Token'")
        error("Then run: export VAULT_ROOT_TOKEN=<token>")
        from invoke.exceptions import Exit

        raise Exit("Invalid root token", code=1)

    token_info = json.loads(validate_result.stdout)
    ttl = token_info.get("data", {}).get("ttl", 0)

    if ttl > 0 and ttl < 300:
        warning(f"Root token expires in {ttl}s. Consider renewing.")

    success("Root token validated")
    print("")

    failed_services = []

    current_project = None
    for target in targets:
        if target.project != current_project:
            print(f"\n--- {target.project} ---")
            current_project = target.project

        policy = vault_policy_name(target.project, env_name, target.service)
        display = vault_token_display_name(target.project, env_name, target.service)
        policy_path = os.path.join(
            target.project_dir, target.service_dir, "vault-policy.hcl"
        )
        previous_accessor = _read_tracked_accessor(
            c,
            vault_addr=vault_addr,
            root_token=root_token,
            project=target.project,
            env_name=env_name,
            service=target.service,
        )

        if os.path.exists(policy_path):
            with open(policy_path, "r") as f:
                policy_rules = f.read().replace("{{env}}", env_name)
            info(f"Loaded tailored policy from {target.service_dir}/vault-policy.hcl")
        else:
            policy_rules = f"""path "auth/token/lookup-self" {{
  capabilities = ["read"]
}}

path "secret/data/{target.project}/{env_name}/{target.service}" {{
  capabilities = ["read", "list"]
}}"""
            warning(
                f"No policy file found for {target.service}, using default read-only"
            )

        # Write policy via vault CLI using stdin
        policy_io = io.StringIO(policy_rules)

        info(f"   Writing policy: {policy}...")
        result = c.run(
            f"vault policy write {policy} -",
            env=_vault_env(vault_addr, root_token),
            in_stream=policy_io,
            hide=True,
            warn=True,
        )
        if not result.ok:
            stderr_msg = getattr(result, "stderr", "") or ""
            if stderr_msg.strip():
                error(
                    f"Failed to create policy '{policy}' for service '{target.service}'",
                    stderr_msg.strip(),
                )
            else:
                error(
                    f"Failed to create policy '{policy}' for service '{target.service}'"
                )
            failed_services.append(
                (target.project, target.service, "policy_write_failed")
            )
            continue
        success(f"   Policy {policy} created")

        # Generate periodic token (orphan, renewable indefinitely via -period).
        # The policy, display name, and tracked accessor are scoped by
        # {project, env, service}; this prevents staging tokens from reading
        # production secrets and lets rotations revoke only the old token.
        cmd = (
            f"vault token create "
            f"-orphan "
            f"-period={TOKEN_PERIOD} "
            f"-policy={policy} "
            f"-no-default-policy "
            f"-display-name={display} "
            f"-format=json"
        )
        result = c.run(
            cmd,
            env=_vault_env(vault_addr, root_token),
            hide=True,
            warn=True,
        )

        if result.ok:
            token_data = json.loads(result.stdout)
            token = token_data["auth"]["client_token"]
            accessor = token_data["auth"].get("accessor", "")
            success(f"Token for {display}: {token_for_output(token)}")

            dokploy_configured = True
            previous_dokploy_token = None
            if deploy:
                configure_result = _configure_dokploy_token(
                    c, target.service, token, target.dokploy_project
                )
                if isinstance(configure_result, dict):
                    dokploy_configured = bool(configure_result.get("configured"))
                    previous_dokploy_token = configure_result.get("previous_token")
                else:
                    dokploy_configured = bool(configure_result)
                if not dokploy_configured and strict_dokploy:
                    failed_services.append(
                        (target.project, target.service, "dokploy_config_failed")
                    )

            if deploy and dokploy_configured:
                tracked = _write_tracked_accessor(
                    c,
                    vault_addr=vault_addr,
                    root_token=root_token,
                    project=target.project,
                    env_name=env_name,
                    service=target.service,
                    accessor=accessor,
                    policy=policy,
                    display=display,
                )
                if not tracked:
                    failed_services.append(
                        (target.project, target.service, "accessor_tracking_failed")
                    )
                elif revoke_old:
                    old_accessor = previous_accessor or _lookup_accessor_for_token(
                        c,
                        vault_addr=vault_addr,
                        token=previous_dokploy_token,
                    )
                    _revoke_accessor(
                        c,
                        vault_addr=vault_addr,
                        root_token=root_token,
                        accessor=old_accessor,
                        new_accessor=accessor,
                    )
            print()
        else:
            error(f"Failed to create token for {target.service}")
            failed_services.append(
                (target.project, target.service, "token_creation_failed")
            )

    if failed_services:
        print("")
        error("❌ Some services failed:")
        for proj, svc, reason in failed_services:
            error(f"  - {proj}/{svc}: {reason}")
        from invoke.exceptions import Exit

        raise Exit("Token generation failed for some services", code=1)

    success("All tokens generated!")
    info("Next steps:")
    info("1. Store tokens in 1Password (optional but recommended)")
    info("2. Set VAULT_APP_TOKEN in Dokploy for each service")
    info("3. Run: invoke <service>.setup")


@task
def setup(c):
    """Complete Vault setup flow"""
    # Check if already running
    if VaultDeployer.check_status(c, None):
        success("Vault already healthy - skipping setup")
        return

    deploy(c)
    init(c)
    unseal(c)
    success("Vault setup complete!")


def _env_value(env_text: str, key: str) -> str | None:
    prefix = f"{key}="
    for line in env_text.splitlines():
        if line.startswith(prefix):
            return line.split("=", 1)[1].strip()
    return None


def _configure_dokploy_token(_c, service: str, token: str, project: str = "platform"):
    """Auto-configure VAULT_APP_TOKEN in Dokploy"""
    try:
        from libs.dokploy import get_dokploy
        from libs.common import get_env

        e = get_env()
        domain = e.get("INTERNAL_DOMAIN")
        env_name = e.get("ENV", "production")
        host = f"cloud.{domain}" if domain else None
        client = get_dokploy(host=host)

        # Find compose service
        compose = client.find_compose_by_name(service, project, env_name=env_name)
        if compose:
            compose_id = compose["composeId"]
            previous_token = _env_value(str(compose.get("env", "")), "VAULT_APP_TOKEN")
            info("   Configuring in Dokploy...")

            client.update_compose_env(compose_id, env_vars={"VAULT_APP_TOKEN": token})
            info("   Updated environment variables")

            # Trigger redeploy to apply changes
            info("   Triggering redeploy...")
            client.deploy_compose(compose_id)
            success("   Auto-configured in Dokploy and redeployed")
            return {"configured": True, "previous_token": previous_token}
        else:
            warning(
                f"   Service '{service}' not found in Dokploy project '{project}', manual setup required"
            )
            return {"configured": False, "previous_token": None}
    except Exception as exc:
        warning(f"   Auto-config failed: {exc}")
        info("   Manual setup: Add VAULT_APP_TOKEN in Dokploy UI")
        return {"configured": False, "previous_token": None}
