"""
1Password Connect deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
Bootstrap layer uses 1Password for secrets, not Vault.
"""
from invoke import task
from libs.common import get_env, validate_env
from libs.console import header, success, error, warning, prompt_action, run_with_status


class OnePasswordDeployer:
    """1Password Connect deployer using libs/ system"""
    
    service = "1password"
    compose_path = "bootstrap/04.1password/compose.yaml"
    data_path = "/data/bootstrap/1password"
    
    @classmethod
    def env(cls):
        return get_env()
    
    @classmethod
    def pre_compose(cls, c) -> bool:
        """Prepare data directory and upload credentials"""
        if missing := validate_env():
            error(f"Missing: {', '.join(missing)}")
            return False
        
        e = cls.env()
        header("1Password pre_compose", "Preparing")
        
        # Create directory
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'mkdir -p {cls.data_path}'", "Create directory")
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'chown -R 1000:1000 {cls.data_path}'", "Set ownership")
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'chmod 777 {cls.data_path}'", "Set permissions")
        
        # Upload credentials from 1Password
        header("1Password credentials", "Uploading from 1Password CLI")
        cmd = f"op document get 'bootstrap/1password/VPS-01 Credentials File' --vault Infra2 | ssh root@{e['VPS_HOST']} 'cat > {cls.data_path}/1password-credentials.json && chown 1000:1000 {cls.data_path}/1password-credentials.json'"
        
        result = c.run(cmd, warn=True)
        if not result.ok:
            error("Upload failed", "Ensure 1Password CLI is configured")
            return False
        
        success("pre_compose complete")
        return True
    
    @classmethod
    def composing(cls, c):
        """Deploy in Dokploy"""
        e = cls.env()
        header("1Password composing", "Deploy in Dokploy")
        prompt_action("Deploy in Dokploy", [
            f"Access: https://cloud.{e['INTERNAL_DOMAIN']}",
            "Project: bootstrap",
            f"Compose: {cls.compose_path}",
            "Click Deploy"
        ])
        success("composing complete")
    
    @classmethod
    def post_compose(cls, c) -> bool:
        """Verify deployment"""
        e = cls.env()
        header("1Password post_compose", "Verifying")
        
        result = c.run(f"curl -s https://op.{e['INTERNAL_DOMAIN']}/health", warn=True)
        if result.ok and "1Password Connect" in result.stdout:
            success("1Password Connect is healthy")
            return True
        warning("Service may need a few minutes to start")
        return False


@task
def prepare(c):
    """Prepare 1Password data directory"""
    OnePasswordDeployer.pre_compose(c)


@task
def upload_credentials(c):
    """Upload 1Password credentials file"""
    e = get_env()
    header("1Password credentials", "Uploading")
    cmd = f"op document get 'bootstrap/1password/VPS-01 Credentials File' --vault Infra2 | ssh root@{e['VPS_HOST']} 'cat > /data/bootstrap/1password/1password-credentials.json && chown 1000:1000 /data/bootstrap/1password/1password-credentials.json'"
    result = c.run(cmd, warn=True)
    if result.ok:
        success("Credentials uploaded")
    else:
        error("Upload failed")


@task(pre=[prepare])
def deploy(c):
    """Deploy 1Password Connect to Dokploy"""
    OnePasswordDeployer.composing(c)


@task(pre=[deploy])
def verify(c):
    """Verify 1Password Connect functionality"""
    OnePasswordDeployer.post_compose(c)


@task
def status(c):
    """Check 1Password Connect status"""
    from libs.console import success, warning
    e = get_env()
    header("1Password status", "Checking")
    
    # Check containers
    result = c.run(f"ssh root@{e['VPS_HOST']} 'docker ps --format \"{{{{.Names}}}} {{{{.Status}}}}\" | grep op-connect'", warn=True, hide=True)
    if result.ok:
        for line in result.stdout.strip().split('\n'):
            if line:
                success(f"Container: {line}")
    else:
        warning("No op-connect containers found")
    
    # Check internal health (via container network)
    result = c.run(f"ssh root@{e['VPS_HOST']} 'docker exec $(docker ps -qf name=op-connect-api) wget -qO- http://localhost:8080/heartbeat 2>/dev/null || echo \"unavailable\"'", warn=True, hide=True)
    if "unavailable" not in result.stdout:
        success(f"Health: {result.stdout.strip()[:50]}")
    else:
        warning("Health check unavailable")


@task
def fix_permissions(c):
    """Fix database permission issues"""
    e = get_env()
    run_with_status(c, f"ssh root@{e['VPS_HOST']} 'chmod 777 /data/bootstrap/1password'", "Fix permissions")


@task(pre=[prepare, deploy, verify])
def setup(c):
    """Complete 1Password Connect setup flow"""
    success("1Password Connect setup complete!")
