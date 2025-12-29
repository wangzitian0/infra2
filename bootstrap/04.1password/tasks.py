"""
1Password Connect deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
Bootstrap layer uses 1Password for secrets, not Vault.
"""
import sys
from invoke import task
from libs.common import get_env
from libs.console import header, success, error, warning, prompt_action, run_with_status
from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("bootstrap.04.1password.shared")


class OnePasswordDeployer(Deployer):
    """1Password Connect deployer using libs/ system"""
    service = "1password"
    compose_path = "bootstrap/04.1password/compose.yaml"
    data_path = "/data/bootstrap/1password"
    uid = "1000"
    gid = "1000"
    chmod = "750"

    @classmethod
    def _upload_credentials(cls, c) -> bool:
        """Upload credentials from 1Password to server."""
        e = cls.env()
        ssh_user = e.get("VPS_SSH_USER") or "root"
        header("1Password credentials", "Uploading from 1Password CLI")
        cmd = (
            "op document get 'bootstrap/1password/VPS-01 Credentials File' --vault Infra2 "
            f"| ssh {ssh_user}@{e['VPS_HOST']} "
            f"'cat > {cls.data_path}/1password-credentials.json && chown {cls.uid}:{cls.gid} {cls.data_path}/1password-credentials.json'"
        )
        result = c.run(cmd, warn=True)
        if not result.ok:
            error("Upload failed", "Ensure 1Password CLI is configured")
            return False
        success("Credentials uploaded")
        return True
    
    @classmethod
    def env(cls):
        return get_env()
    
    @classmethod
    def pre_compose(cls, c) -> bool:
        """Prepare data directory and upload credentials"""
        if not cls._prepare_dirs(c):
            return False
        if not cls._upload_credentials(c):
            return False

        success("pre-compose complete")
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
        header("1Password post-compose", "Verifying")
        
        result = c.run(f"curl -s https://op.{e['INTERNAL_DOMAIN']}/health", warn=True)
        if result.ok and "1Password Connect" in result.stdout:
            success("1Password Connect is healthy")
            return True
        warning("Service may need a few minutes to start")
        return False


if shared_tasks:
    _tasks = make_tasks(OnePasswordDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]


@task
def prepare(c):
    """Prepare 1Password data directory"""
    OnePasswordDeployer._prepare_dirs(c)


@task
def upload_credentials(c):
    """Upload 1Password credentials file"""
    OnePasswordDeployer._upload_credentials(c)


@task(pre=[prepare])
def deploy(c):
    """Deploy 1Password Connect to Dokploy"""
    OnePasswordDeployer.composing(c)


@task(pre=[deploy])
def verify(c):
    """Verify 1Password Connect functionality"""
    OnePasswordDeployer.post_compose(c)


@task
def fix_permissions(c):
    """Fix database permission issues"""
    e = get_env()
    ssh_user = e.get("VPS_SSH_USER") or "root"
    run_with_status(c, f"ssh {ssh_user}@{e['VPS_HOST']} 'chmod 750 /data/bootstrap/1password'", "Fix permissions")
