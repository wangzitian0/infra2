"""
1Password Connect deployment automation tasks
Uses libs/ system for consistent environment and console utilities.
Bootstrap layer uses 1Password for secrets, not Vault.
"""
import sys
from invoke import task
from libs.common import get_env
from libs.console import header, success, error, warning, info, prompt_action, run_with_status
from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("bootstrap.04.1password.shared")


class OnePasswordDeployer(Deployer):
    """1Password Connect deployer using libs/ system"""
    service = "1password"
    compose_path = "bootstrap/04.1password/compose.yaml"
    data_path = "/data/bootstrap/1password"
    uid = "999"
    gid = "999"
    chmod = "700"

    # Repository config (could be moved to libs/config later)
    GITHUB_OWNER = "wangzitian0"
    GITHUB_REPO = "infra2"
    GITHUB_BRANCH = "main"

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
    def _get_github_provider_id(cls, client) -> str | None:
        """Get GitHub provider ID by querying API or finding existing."""
        # Method 1: Query API directly
        try:
            providers = client.list_git_providers()
            for p in providers:
                if p.get("provider") == "github":
                    return p.get("gitProviderId")
        except Exception:
            # Ignore API errors if method not supported or fails
            pass

        # Method 2: Infer from existing services
        try:
            projects = client.list_projects()
            for proj in projects:
                for env in proj.get('environments', []):
                    for comp in env.get('compose', []):
                        if comp.get('githubId'):
                            return comp.get('githubId')
        except Exception:
            # Ignore errors during project enumeration
            pass
            
        return None

    @classmethod
    def composing(cls, c, env_vars: dict = None) -> bool:
        """Deploy in Dokploy (automated)"""
        if not isinstance(env_vars, dict):
             from libs.common import get_env
             env_vars = get_env()
             
        e = cls.env()
        header("1Password composing", "Deploy in Dokploy (automated)")
        
        try:
            from libs.dokploy import ensure_project, get_dokploy
            
            # Ensure project exists
            domain = env_vars.get('INTERNAL_DOMAIN')
            host = f"cloud.{domain}" if domain else None
            
            project_id, env_id = ensure_project("bootstrap", "Bootstrap infrastructure services", host=host)
            if not env_id:
                error("Failed to get environment ID")
                return False
            
            info(f"Project: bootstrap (env: {env_id})")
            
            # Deploy compose using GitHub provider
            client = get_dokploy(host=host)
            existing = client.find_compose_by_name("1password-connect", "bootstrap")
            
            # GitHub repository info
            compose_path = cls.compose_path
            
            # Get GitHub provider ID
            github_id = cls._get_github_provider_id(client)
            if not github_id:
                raise ValueError("No GitHub provider configured in Dokploy. Please add one in Settings -> Git Providers.")
                
            info(f"Using GitHub provider: {github_id}")
            
            if existing:
                compose_id = existing["composeId"]
                info("Updating existing compose service")
                client.update_compose(compose_id, source_type="github")
            else:
                info("Creating new compose service with GitHub provider")
                result = client._request("POST", "compose.create", json={
                    "name": "1password-connect",
                    "appName": "bootstrap-1password",
                    "environmentId": env_id,
                    "sourceType": "github",
                    "githubId": github_id,
                    "repository": f"{cls.GITHUB_OWNER}/{cls.GITHUB_REPO}",
                    "branch": cls.GITHUB_BRANCH,
                    "composePath": compose_path,
                })
                compose_id = result["composeId"]
            
            # Update environment variables
            info("Updating environment variables (from libs)")
            env_content = "\n".join([f"{k}={v}" for k, v in env_vars.items() if v is not None])
            client.update_compose(compose_id, env=env_content)
            
            info(f"Deploying compose (ID: {compose_id})")
            client.deploy_compose(compose_id)
            
            success("1Password Connect deployment triggered")
            warning("Wait 1-2 minutes for containers to start")
            return True
            
        except Exception as ex:
            error(f"Deployment failed: {ex}")
            warning("Falling back to manual deployment")
            prompt_action("Deploy in Dokploy manually", [
                f"Access: https://cloud.{e['INTERNAL_DOMAIN']}",
                "Project: bootstrap",
                f"Compose: {cls.compose_path}",
                "Click Deploy"
            ])
            return False
    
    @classmethod
    def post_compose(cls, c, shared_tasks=None) -> bool:
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
