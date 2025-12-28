"""
Vault deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
"""
from invoke import task
from libs.common import get_env, validate_env
from libs.console import header, success, error, warning, prompt_action, run_with_status


class VaultDeployer:
    """Vault deployer using libs/ system"""
    
    service = "vault"
    compose_path = "bootstrap/05.vault/compose.yaml"
    data_path = "/data/bootstrap/vault"
    
    @classmethod
    def env(cls):
        return get_env()
    
    @classmethod
    def pre_compose(cls, c) -> bool:
        """Prepare data directory and upload config"""
        if missing := validate_env():
            error(f"Missing: {', '.join(missing)}")
            return False
        
        e = cls.env()
        header("Vault pre_compose", "Preparing")
        
        # Create directories
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/{{file,logs,config}}'", "Create directories")
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'chown -R 1000:1000 {cls.data_path}'", "Set ownership")
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'chmod 755 {cls.data_path}'", "Set permissions")
        
        # Upload config
        run_with_status(c, f"scp bootstrap/05.vault/vault.hcl root@{e['VPS_HOST']}:{cls.data_path}/config/", "Upload config")
        
        success("pre_compose complete")
        return True
    
    @classmethod
    def composing(cls, c):
        """Deploy in Dokploy"""
        e = cls.env()
        header("Vault composing", "Deploy in Dokploy")
        prompt_action("Deploy in Dokploy", [
            f"Access: https://cloud.{e['INTERNAL_DOMAIN']}",
            "Project: bootstrap",
            f"Compose: {cls.compose_path}",
            "Ensure OP_CONNECT_TOKEN is configured",
            "Click Deploy"
        ])
        success("composing complete")
    
    @classmethod
    def post_compose(cls, c) -> bool:
        """Verify deployment"""
        e = cls.env()
        header("Vault post_compose", "Verifying")
        
        result = c.run(f"curl -s https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health", warn=True)
        if result.ok:
            success("Vault is reachable")
            return True
        warning("Vault may need initialization")
        return False


@task
def prepare(c):
    """Prepare Vault data directory"""
    VaultDeployer.pre_compose(c)


@task
def upload_config(c):
    """Upload Vault configuration"""
    e = get_env()
    run_with_status(c, f"scp bootstrap/05.vault/vault.hcl root@{e['VPS_HOST']}:/data/bootstrap/vault/config/", "Upload config")


@task(pre=[prepare])
def deploy(c):
    """Deploy Vault to Dokploy (Manual steps)"""
    VaultDeployer.composing(c)


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
    header("Vault unseal", "Triggering unsealer")
    c.run(f"ssh root@{e['VPS_HOST']} 'docker logs --tail 20 vault-unsealer'", warn=True)
    c.run(f"ssh root@{e['VPS_HOST']} 'docker restart vault-unsealer'")
    success("Unsealer restarted")


@task
def status(c):
    """Check Vault status"""
    e = get_env()
    header("Vault status", "Checking")
    c.run(f"curl -s https://vault.{e['INTERNAL_DOMAIN']}/v1/sys/health", warn=True)
    c.run(f"ssh root@{e['VPS_HOST']} 'docker ps | grep vault'", warn=True)


@task(pre=[prepare, deploy, init, unseal])
def setup(c):
    """Complete Vault setup flow"""
    success("Vault setup complete!")
