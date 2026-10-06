"""
Vault deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
"""

import json
import os
import re
import time

from invoke import task
from libs.deploy.deployer import Deployer
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
from libs.security.vault_tokens import (
    VaultTokenTarget,
    normalize_selector,
    policy_name as vault_policy_name,
)
from typing import Any

CONNECT_TOKEN_ITEM = "infra2.0 Access Token: infra2.0"


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
            from libs.security.store import OpSecrets

            vault_result = c.run(
                "op vault get Infra2 --format json", hide=True, warn=True
            )
            if vault_result.ok:
                vault = json.loads(vault_result.stdout)
                env_vars["OP_VAULT_ID"] = vault["id"]
            else:
                error("Failed to resolve Infra2 vault ID for 1Password Connect")
                return None

            # OP_CONNECT_TOKEN (from the 1Password Connect server token)
            # Item: "infra2.0 Access Token: infra2.0"
            token_item = OpSecrets(item=CONNECT_TOKEN_ITEM)
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
            error("Missing libs.security.store dependencies")
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
        from libs.deploy.dokploy_client import ensure_project, get_dokploy
        from libs.core.constants import GITHUB_BRANCH, GITHUB_OWNER, GITHUB_REPO

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
            elif result["skipped"] > 0:
                info(f"Domain already configured: {domain_host}")

        info(f"Deploying compose {compose_id}...")
        client.deploy_compose(compose_id)

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
                "minio": "03.s3",
                "authentik": "10.authentik",
                "alerting": "12.alerting",
                "prefect": "23.prefect",
                "openpanel": "24.openpanel",
                "todo": "30.todo",
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
        (
            "truealpha",
            os.path.join(root_dir, "truealpha", "truealpha"),
            {
                "postgres": "01.postgres",
                "app": "10.app",
                # data_engine was missing from this map, so `invoke
                # vault.setup-approle --project=truealpha --service=data_engine`
                # answered "No matching AppRole targets" — found during the
                # 2026-07-27 production graduation; its SecretsFacet has always
                # declared approle auth for all three dagster containers.
                "data_engine": "20.data_engine",
            },
            "truealpha",
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


def _read_policy_file(policy_path: str, env_name: str) -> str:
    """Read a service's vault-policy.hcl, substituting ``{{env}}``. The one bit shared by
    the AppRole setup path (setup_approle); substitutes the ``{{env}}`` placeholder."""
    with open(policy_path) as f:
        return f.read().replace("{{env}}", env_name)


def _redeploy_with_vault_creds(
    service: str, env_vars: dict[str, str], project: str
) -> dict[str, Any] | None:
    """Shared spine for injecting Vault creds into a Dokploy service and redeploying.

    Finds the ``service`` compose in ``project``, merges ``env_vars`` into its runtime env,
    triggers a redeploy, and waits for the new deployment record. Returns the *pre-update*
    compose dict (so callers can read the previous env), or ``None`` if the service is not
    registered in Dokploy. Raises on Dokploy/API errors — callers own how to report them.
    Backs ``_configure_dokploy_approle`` (VAULT_ROLE_ID/VAULT_SECRET_ID).
    """
    client, env_name = _dokploy_client()
    compose = client.find_compose_by_name(service, project, env_name=env_name)
    if not compose:
        return None
    compose_id = compose["composeId"]
    client.update_compose_env(compose_id, env_vars=env_vars)
    info("   Triggering redeploy and waiting for runtime deployment record...")
    _deploy_compose_with_record_check(client, compose_id)
    return compose


def _dokploy_client() -> tuple[Any, str]:
    """The Dokploy client for this environment, and the environment name."""
    from libs.deploy.dokploy_client import get_dokploy
    from libs.common import get_env

    e = get_env()
    domain = e.get("INTERNAL_DOMAIN")
    host = f"cloud.{domain}" if domain else None
    return get_dokploy(host=host), e.get("ENV", "production")


def _deploy_compose_with_record_check(client: Any, compose_id: str) -> None:
    """Apply a Dokploy env update and fail if runtime deployment stays stale."""
    timeout = int(os.getenv("DOKPLOY_DEPLOYMENT_RECORD_TIMEOUT_SECONDS", "90"))
    interval = int(os.getenv("DOKPLOY_DEPLOYMENT_RECORD_INTERVAL_SECONDS", "5"))

    before_ids = _deployment_ids(_safe_compose(client, compose_id))
    client.deploy_compose(compose_id)
    if _wait_for_new_deployment_record(
        client, compose_id, before_ids, timeout, interval
    ):
        return

    warning(
        "   Dokploy deploy did not produce a new deployment record; retrying compose.redeploy"
    )
    before_ids = _deployment_ids(_safe_compose(client, compose_id))
    client.redeploy_compose(compose_id)
    if _wait_for_new_deployment_record(
        client, compose_id, before_ids, timeout, interval
    ):
        return

    raise RuntimeError(
        "Dokploy deploy/redeploy did not produce a new deployment record; "
        "VAULT_APP_TOKEN may be updated in Dokploy env but not applied to running containers"
    )


def _safe_compose(client: Any, compose_id: str) -> dict[str, Any]:
    try:
        data = client.get_compose(compose_id)
    except Exception:  # noqa: BLE001 - setup must classify stale Dokploy state.
        return {}
    return data if isinstance(data, dict) else {}


def _deployment_ids(compose: dict[str, Any]) -> set[str]:
    deployments = compose.get("deployments")
    if not isinstance(deployments, list):
        return set()
    return {
        str(deployment.get("deploymentId") or deployment.get("id") or "")
        for deployment in deployments
        if isinstance(deployment, dict)
        and (deployment.get("deploymentId") or deployment.get("id"))
    }


def _wait_for_new_deployment_record(
    client: Any,
    compose_id: str,
    previous_ids: set[str],
    timeout_seconds: int,
    interval_seconds: int,
) -> bool:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while True:
        compose = _safe_compose(client, compose_id)
        deployments = compose.get("deployments")
        new_ids = _deployment_ids(compose) - previous_ids
        if new_ids and isinstance(deployments, list):
            for deployment in deployments:
                deployment_id = str(
                    deployment.get("deploymentId") or deployment.get("id") or ""
                )
                status = str(deployment.get("status") or "")
                if deployment_id in new_ids and status == "error":
                    return False
                if deployment_id in new_ids and status == "done":
                    return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(1, interval_seconds))


# ---------------------------------------------------------------------------
# AppRole auth setup (replaces token_file periodic tokens; see infra2 #257)
# ---------------------------------------------------------------------------

# The AppRole-issued token is renewed within token_ttl and re-authenticated
# (fresh login) at token_max_ttl, so it never decays the way the old token_file
# periodic tokens did.
APPROLE_TOKEN_TTL = "24h"
APPROLE_TOKEN_MAX_TTL = "168h"


@task(
    help={
        "project": "Limit setup to one project, e.g. finance_report.",
        "service": "Limit setup to one service, e.g. app.",
        "deploy": "Issue a secret id, give it to every compose of the role, redeploy, "
        "then destroy the earlier secret ids. False writes policy and role only.",
    }
)
def setup_approle(c, project=None, service=None, deploy=True):
    """Enable AppRole + per-service role/policy/secret-id, injected into Dokploy.

    Replaces the token_file periodic-token model: each service's vault-agent logs
    in with role_id/secret_id and natively renews/re-auths its token (#257).

    A re-issue ends the earlier secret ids (#1070): after the target and every other
    compose of the role run the new secret id, the task destroys each secret id that
    existed before the run. A failed step destroys nothing and the task exits 1.
    """
    import io

    header("Vault AppRole Setup", "Enabling approle + per-service roles")

    root_token = os.getenv("VAULT_ROOT_TOKEN")
    if not root_token:
        error("VAULT_ROOT_TOKEN not set")
        info(
            "Get from: op read 'op://Infra2/bootstrap/vault/Root Token/Root Token' "
            "then: export VAULT_ROOT_TOKEN=<token>"
        )
        return

    e = get_env()
    vault_addr = e.get("VAULT_ADDR", f"https://vault.{e['INTERNAL_DOMAIN']}")
    env_name = e.get("ENV", "production")
    venv = _vault_env(vault_addr, root_token)
    success(f"Using Vault: {vault_addr} (env={env_name})")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    root_dir = os.path.dirname(os.path.dirname(current_dir))
    target_project = normalize_selector(project, label="project")
    target_service = normalize_selector(service, label="service")
    targets = _select_token_targets(
        _vault_token_targets(root_dir), target_project, target_service
    )
    if not targets:
        from invoke.exceptions import Exit

        raise Exit("No matching AppRole targets", code=1)
    # The runner's own redeploy stops a run inside the runner (SOP-007), so it goes
    # last: every other role completes first.
    targets.sort(key=lambda t: (t.project, t.service) == ("bootstrap", "iac_runner"))

    if not c.run("vault token lookup", env=venv, hide=True, warn=True).ok:
        from invoke.exceptions import Exit

        raise Exit("VAULT_ROOT_TOKEN is invalid or expired", code=1)

    listed = c.run("vault auth list -format=json", env=venv, hide=True, warn=True)
    if listed.ok and "approle/" in json.loads(listed.stdout):
        info("approle auth already enabled")
    else:
        if not c.run("vault auth enable approle", env=venv, hide=True, warn=True).ok:
            from invoke.exceptions import Exit

            raise Exit("Failed to enable approle auth", code=1)
        success("approle auth enabled")

    failed = []
    current_project = None
    for target in targets:
        if target.project != current_project:
            print(f"\n--- {target.project} ---")
            current_project = target.project

        policy = vault_policy_name(target.project, env_name, target.service)
        role = policy  # one approle role per policy/service/env
        policy_path = os.path.join(
            target.project_dir, target.service_dir, "vault-policy.hcl"
        )
        if os.path.exists(policy_path):
            policy_rules = _read_policy_file(policy_path, env_name)
        else:
            policy_rules = (
                f'path "secret/data/{target.project}/{env_name}/{target.service}" {{\n'
                f'  capabilities = ["read", "list"]\n}}'
            )
            warning(f"No policy file for {target.service}, using default read-only")

        if not c.run(
            f"vault policy write {policy} -",
            env=venv,
            in_stream=io.StringIO(policy_rules),
            hide=True,
            warn=True,
        ).ok:
            error(f"Failed to write policy {policy}")
            failed.append((target.project, target.service, "policy_write_failed"))
            continue

        if not c.run(
            f"vault write auth/approle/role/{role} "
            f"token_policies={policy} token_no_default_policy=true "
            f"token_ttl={APPROLE_TOKEN_TTL} token_max_ttl={APPROLE_TOKEN_MAX_TTL} "
            f"secret_id_num_uses=0 secret_id_ttl=0",
            env=venv,
            hide=True,
            warn=True,
        ).ok:
            error(f"Failed to write approle role {role}")
            failed.append((target.project, target.service, "role_write_failed"))
            continue

        if not deploy:
            # Nothing would store or print a secret id issued here, so it would only
            # add one more valid credential that no service uses (#1070).
            info(f"   Role {role}: policy and role written; no secret id issued")
            continue

        # The accessors from before this run. They are destroyed only after every
        # consumer of the role runs the new secret id (#1070).
        prior_accessors = _secret_id_accessors(c, venv, role)
        if prior_accessors is None:
            error(f"Failed to list the existing secret ids of {role}")
            failed.append((target.project, target.service, "secret_id_list_failed"))
            continue

        role_id_res = c.run(
            f"vault read -format=json auth/approle/role/{role}/role-id",
            env=venv,
            hide=True,
            warn=True,
        )
        secret_id_res = c.run(
            f"vault write -f -format=json auth/approle/role/{role}/secret-id",
            env=venv,
            hide=True,
            warn=True,
        )
        if not role_id_res.ok or not secret_id_res.ok:
            error(f"Failed to obtain role-id/secret-id for {role}")
            failed.append((target.project, target.service, "credential_failed"))
            continue
        role_id = json.loads(role_id_res.stdout)["data"]["role_id"]
        issued = json.loads(secret_id_res.stdout)["data"]
        secret_id = issued["secret_id"]
        new_accessor = issued["secret_id_accessor"]
        success(f"   Role {role}: role_id + secret_id ready")

        if not _configure_dokploy_approle(
            c, target.service, role_id, secret_id, target.dokploy_project
        ):
            failed.append((target.project, target.service, "dokploy_config_failed"))
            warning(f"   {role}: earlier secret ids stay valid; nothing destroyed")
            continue
        if not _refresh_role_consumers(role_id, secret_id):
            failed.append((target.project, target.service, "consumer_refresh_failed"))
            warning(f"   {role}: earlier secret ids stay valid; nothing destroyed")
            continue
        stale = [a for a in prior_accessors if a != new_accessor]
        if not _destroy_secret_ids(c, venv, role, stale):
            failed.append((target.project, target.service, "secret_id_destroy_failed"))

    if failed:
        error("Some services failed:")
        for proj, svc, reason in failed:
            error(f"  - {proj}/{svc}: {reason}")
        from invoke.exceptions import Exit

        raise Exit("AppRole setup failed for some services", code=1)

    success("AppRole setup complete!")


def _configure_dokploy_approle(
    _c, service: str, role_id: str, secret_id: str, project: str = "platform"
) -> bool:
    """Inject VAULT_ROLE_ID/VAULT_SECRET_ID into Dokploy env and redeploy."""
    try:
        compose = _redeploy_with_vault_creds(
            service,
            {"VAULT_ROLE_ID": role_id, "VAULT_SECRET_ID": secret_id},
            project,
        )
        if compose is None:
            warning(f"   Service '{service}' not found in Dokploy project '{project}'")
            return False
        success("   Auto-configured in Dokploy and runtime redeploy recorded")
        return True
    except Exception as exc:  # noqa: BLE001 - report and let caller mark failure.
        error(f"   Dokploy approle config failed: {exc}")
        return False


# A Vault secret id accessor is a UUID. The check keeps a value from Vault output
# out of a shell command unless it has this shape.
_ACCESSOR_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")


def _secret_id_accessors(c, venv: dict[str, str], role: str) -> list[str] | None:
    """The accessors of every secret id of ``role``, or None when Vault did not answer.

    ``vault list -format=json`` prints ``{}`` and exits 2 when the role has no secret
    id; any other failure prints no JSON.
    """
    res = c.run(
        f"vault list -format=json auth/approle/role/{role}/secret-id",
        env=venv,
        hide=True,
        warn=True,
    )
    try:
        data = json.loads(res.stdout or "")
    except ValueError:
        return None
    if data == {}:
        return []
    if (
        res.ok
        and isinstance(data, list)
        and all(isinstance(a, str) and _ACCESSOR_RE.match(a) for a in data)
    ):
        return data
    return None


def _env_value(env_str: str, key: str) -> str:
    """The value of ``key`` in a Dokploy env string, without quotes.

    A key that occurs twice has no single value: Dokploy merges by the exact name
    and the container reads the last line, so the task cannot know what runs.
    """
    values = []
    for line in env_str.splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() == key:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values.append(value)
    if len(values) > 1:
        raise ValueError(f"{key} occurs {len(values)} times in one compose env")
    return values[0] if values else ""


def _scan_role_consumers(client: Any, role_id: str) -> list[tuple[str, str, str]]:
    """``(composeId, label, VAULT_SECRET_ID)`` of each compose that logs in as ``role_id``."""
    found = []
    for project in client.list_projects():
        for environment in project.get("environments") or []:
            for compose in environment.get("compose") or []:
                compose_id = compose.get("composeId")
                if not compose_id:
                    raise RuntimeError(
                        f"compose {compose.get('name')!r} has no composeId"
                    )
                env_str = client.get_compose_env(compose_id)
                if _env_value(env_str, "VAULT_ROLE_ID") != role_id:
                    continue
                label = f"{project.get('name')}/{environment.get('name')}/{compose.get('name')}"
                found.append(
                    (compose_id, label, _env_value(env_str, "VAULT_SECRET_ID"))
                )
    return found


def _refresh_role_consumers(role_id: str, secret_id: str) -> bool:
    """Give the new secret id to every Dokploy compose that logs in as ``role_id``.

    A PR preview copies the AppRole credentials of its source environment
    (``libs/deploy/preview.py``), so the target compose is not the only consumer of a
    role. A consumer that keeps a destroyed secret id fails its next login. Returns
    True only when every consumer has the new secret id and a new ``done``
    deployment record, and a second read finds no earlier secret id.
    """
    try:
        client, _env_name = _dokploy_client()
        consumers = _scan_role_consumers(client, role_id)
        # The target already holds the new secret id. A scan that misses it cannot
        # be complete (for example, a changed project.all shape).
        if not any(held == secret_id for _, _, held in consumers):
            raise RuntimeError("the scan did not find the target compose of the role")
        blocked = []
        for compose_id, label, held in consumers:
            if held == secret_id:
                continue
            info(f"   Consumer {label}: new secret id, redeploying")
            try:
                client.update_compose_env(
                    compose_id, env_vars={"VAULT_SECRET_ID": secret_id}
                )
                _deploy_compose_with_record_check(client, compose_id)
            except Exception as exc:  # noqa: BLE001 - collect every blocked consumer.
                error(f"   Consumer {label}: {exc}")
                blocked.append(label)
        if blocked:
            error(
                "   These composes did not finish the switch to the new secret id: "
                f"{', '.join(blocked)}. Repair or tear down each one (a preview: "
                "`python -m tools.deploy_v2 ... --down`), then run the task again."
            )
            return False
        # A deploy or a preview `up` that read the earlier env can write it back after
        # the scan. Read again just before the destroy.
        stale = [
            label
            for _, label, held in _scan_role_consumers(client, role_id)
            if held != secret_id
        ]
        if stale:
            error(
                f"   An earlier secret id came back on: {', '.join(stale)}. "
                "Run the task again when no deploy or preview run is in flight."
            )
            return False
    except Exception as exc:  # noqa: BLE001 - report and let caller mark failure.
        error(f"   Refreshing the consumers of the role failed: {exc}")
        return False
    return True


def _destroy_secret_ids(
    c, venv: dict[str, str], role: str, accessors: list[str]
) -> bool:
    """Destroy each listed secret id of ``role``. True only when all are gone."""
    ok = True
    for accessor in accessors:
        if not c.run(
            f"vault write auth/approle/role/{role}/secret-id-accessor/destroy "
            f"secret_id_accessor={accessor}",
            env=venv,
            hide=True,
            warn=True,
        ).ok:
            error(f"   {role}: failed to destroy secret id accessor {accessor}")
            ok = False
    if ok:
        success(f"   {role}: destroyed {len(accessors)} earlier secret id(s)")
    return ok
