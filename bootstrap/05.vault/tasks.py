"""
Vault deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
"""
from invoke import task
from libs.deployer import Deployer
from libs.common import get_env
from libs.console import header, success, error, warning, info, prompt_action, run_with_status
from typing import Any


class VaultDeployer(Deployer):
    """Vault deployer using libs/ system"""
    
    service = "vault"
    compose_path = "bootstrap/05.vault/compose.yaml"
    data_path = "/data/bootstrap/vault"
    uid = "1000"
    gid = "1000"
    chmod = "755"
    
    @classmethod
    def pre_compose(cls, c) -> bool:
        """Prepare data directory and upload config"""
        if not cls._prepare_dirs(c):
            return False
        e = cls.env()
        header("Vault pre-compose", "Preparing")
        ssh_user = e.get("VPS_SSH_USER") or "root"

        # Create directories
        result = run_with_status(
            c,
            f"ssh {ssh_user}@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/{{file,logs,config}}'",
            "Create directories",
        )
        if not result.ok:
            return False

        if not cls.upload_config(c):
            return False

        success("pre-compose complete")
        return True

    @classmethod
    def upload_config(cls, c) -> bool:
        """Upload Vault config file."""
        e = cls.env()
        ssh_user = e.get("VPS_SSH_USER") or "root"
        result = run_with_status(
            c,
            f"scp bootstrap/05.vault/vault.hcl {ssh_user}@{e['VPS_HOST']}:{cls.data_path}/config/",
            "Upload config",
        )
        return result.ok
    
    @classmethod
    def post_compose(cls, c, shared_tasks: Any) -> bool:
        """Verify deployment"""
        e = cls.env()
        header("Vault post-compose", "Verifying")
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
            error("Vault status check failed: curl command did not complete successfully.")
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


# Standard tasks
# We don't use make_tasks fully because Vault requires extra steps (init, unseal)
prepare = task(lambda c: VaultDeployer.pre_compose(c), name="prepare")
upload_config = task(lambda c: VaultDeployer.upload_config(c), name="upload-config")
deploy = task(lambda c: VaultDeployer.composing(c), name="deploy", pre=[prepare])


@task(pre=[deploy])
def init(c):
    """Initialize Vault"""
    e = get_env()
    header("Vault init", "Initialization required")
    commands = [
        f"export VAULT_ADDR=https://vault.{e['INTERNAL_DOMAIN']}",
        "vault operator init",
    ]
    prompt_action("Initialize Vault", [
        f"Run: {commands[0]}",
        f"Run: {commands[1]}",
        "Save keys to 1Password",
    ])


@task
def unseal(c):
    """(Manual trigger) Restart unsealer container"""
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    header("Vault unseal", "Triggering unsealer")
    c.run(f"ssh {ssh_user}@{e['VPS_HOST']} 'docker logs --tail 20 vault-unsealer'", warn=True)
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


@task
def setup_tokens(c):
    """Generate read-only tokens for platform services"""
    import os
    import json

    header("Vault Token Setup", "Generating service tokens")

    # Check VAULT_ROOT_TOKEN
    root_token = os.getenv("VAULT_ROOT_TOKEN")
    if not root_token:
        error("VAULT_ROOT_TOKEN not set")
        print("\nGet from: op read 'op://Infra2/bootstrap-vault/Root Token'")
        print("Then run: export VAULT_ROOT_TOKEN=<token>")
        return

    e = get_env()
    vault_addr = e.get("VAULT_ADDR", f"https://vault.{e['INTERNAL_DOMAIN']}")

    # Service definitions: service_name -> list of paths
    services = {
        "postgres": ["secret/data/platform/production/postgres"],
        "redis": ["secret/data/platform/production/redis"],
        "authentik": [
            "secret/data/platform/production/postgres",
            "secret/data/platform/production/redis",
            "secret/data/platform/production/authentik",
        ],
    }

    success(f"Using Vault: {vault_addr}")
    print("")

    for service, paths in services.items():
        policy_name = f"platform-{service}-reader"

        # Create policy HCL
        policy_rules = "\n".join([
            f'path "{path}" {{\n  capabilities = ["read"]\n}}'
            for path in paths
        ])

        # Write policy via vault CLI
        warning(f"Creating policy: {policy_name}")
        result = c.run(
            f'echo "{policy_rules}" | vault policy write {policy_name} -',
            env={"VAULT_ADDR": vault_addr, "VAULT_TOKEN": root_token},
            hide=True,
            warn=True,
        )

        # Generate token (permanent, orphan, no default policy)
        cmd = (
            f"vault token create "
            f"-orphan "
            f"-policy={policy_name} "
            f"-no-default-policy "
            f"-display-name=platform-{service} "
            f"-format=json"
        )
        result = c.run(
            cmd,
            env={"VAULT_ADDR": vault_addr, "VAULT_TOKEN": root_token},
            hide=True,
        )

        if result.ok:
            token_data = json.loads(result.stdout)
            token = token_data["auth"]["client_token"]

            success(f"Token for {service}:")
            print(f"   {token}")

            _configure_dokploy_token(service, token)

            print("")
        else:
            error(f"Failed to create token for {service}")

    success("\nAll tokens generated!")
    print("\nNext steps:")
    print("1. Store tokens in 1Password (optional but recommended)")
    print("2. Set VAULT_APP_TOKEN in Dokploy for each service")
    print("3. Run: invoke <service>.setup")


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


def _configure_dokploy_token(service: str, token: str):
    """Auto-configure VAULT_APP_TOKEN in Dokploy"""
    try:
        from libs.dokploy import get_dokploy
    except Exception as exc:
        warning(f"   Dokploy client unavailable: {exc}")
        info("   Manual setup: Add VAULT_APP_TOKEN in Dokploy UI")
        return

    try:
        client = get_dokploy()

        # Find compose service
        compose = client.find_compose_by_name(service, "platform")
        if compose:
            compose_id = compose["composeId"]
            info("   Configuring in Dokploy...")

            client.update_compose_env(
                compose_id,
                env_vars={"VAULT_APP_TOKEN": token}
            )
            success("   Auto-configured in Dokploy")
        else:
            warning(f"   Service '{service}' not found in Dokploy, manual setup required")
    except Exception as exc:
        warning(f"   Auto-config failed: {exc}")
        info("   Manual setup: Add VAULT_APP_TOKEN in Dokploy UI")
