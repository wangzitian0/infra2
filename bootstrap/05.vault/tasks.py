"""
Vault deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
"""
from invoke import task
from libs.deployer import Deployer
from libs.common import get_env
from libs.console import header, success, error, warning, prompt_action, run_with_status
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
        header("Vault pre_compose", "Preparing")
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

        success("pre_compose complete")
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
            return False
        return result.stdout.strip() in {"200", "429", "472", "473"}


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
    print(f"export VAULT_ADDR=https://vault.{e['INTERNAL_DOMAIN']}")
    print("vault operator init")
    prompt_action("Initialize Vault", [
        "Run the commands above",
        "Save keys to 1Password"
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
def setup(c):
    """Complete Vault setup flow"""
    # Check if already running
    if VaultDeployer.check_status(c, None):
        success("Vault already healthy - skipping setup")
        return

    prepare(c)
    deploy(c)
    init(c)
    unseal(c)
    success("Vault setup complete!")
