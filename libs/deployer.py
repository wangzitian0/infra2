"""
Base deployer class with DRY task generation

Uses libs/env.py for secret management.
Key pattern: check status first, skip if healthy.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
from invoke import task
from libs.common import get_env, validate_env
from libs.console import header, success, error, warning, info, env_vars, prompt_action, run_with_status
from libs.env import EnvManager, generate_password

if TYPE_CHECKING:
    from invoke import Context


class Deployer:
    """Base class for service deployment
    
    Key principle: check status first
    - If service healthy → skip setup, preserve existing config
    - If service not ready → can generate new secrets, do full install
    """
    
    service: str = ""
    compose_path: str = ""
    data_path: str = ""
    uid: str = "999"
    gid: str = "999"
    chmod: str = "755"
    secret_key: str = "password"  # Key name in Vault
    env_var_name: str = ""  # Env var to display
    
    @classmethod
    def env(cls) -> dict[str, str | None]:
        return get_env()
    
    @classmethod
    def get_env_manager(cls) -> EnvManager:
        """Get EnvManager for this service"""
        e = cls.env()
        project = e.get('PROJECT', 'platform')
        env = e.get('ENV', 'production')
        return EnvManager(project=project, env=env, service=cls.service)
    
    @classmethod
    def check_status(cls, c: "Context", shared_tasks: Any) -> bool:
        """Check if service is already healthy"""
        try:
            result = shared_tasks.status(c)
            return result.get("is_ready", False)
        except Exception:
            return False
    
    @classmethod
    def _prepare_dirs(cls, c: "Context") -> bool:
        """Create data directories"""
        if missing := validate_env():
            error(f"Missing: {', '.join(missing)}")
            return False
        
        e = cls.env()
        header(f"{cls.service} pre_compose", f"Preparing ({e['ENV']})")
        
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'mkdir -p {cls.data_path}'", "Create directory")
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'chown -R {cls.uid}:{cls.gid} {cls.data_path}'", "Set ownership")
        run_with_status(c, f"ssh root@{e['VPS_HOST']} 'chmod -R {cls.chmod} {cls.data_path}'", "Set permissions")
        return True
    
    @classmethod
    def pre_compose(cls, c: "Context") -> dict | None:
        """Prepare and generate NEW secrets (only call if not healthy)"""
        if not cls._prepare_dirs(c):
            return None
        
        # Generate NEW password (atomic install)
        env_mgr = cls.get_env_manager()
        password = generate_password(24)
        env_mgr.set_secret(cls.secret_key, password)
        
        secrets = {cls.env_var_name: password}
        env_vars("DOKPLOY ENV", secrets)
        success("pre_compose complete")
        return secrets
    
    @classmethod
    def composing(cls, c: "Context", env_keys: list[str] | None = None) -> None:
        """Deploy in Dokploy"""
        e = cls.env()
        keys = env_keys or [cls.env_var_name]
        header(f"{cls.service} composing", f"Deploy {cls.service}")
        prompt_action("Deploy in Dokploy", [
            f"Access: https://cloud.{e['INTERNAL_DOMAIN']}",
            f"Compose: {cls.compose_path}",
            f"Add env: {', '.join(keys)}",
            "Click Deploy"
        ])
        success("composing complete")
    
    @classmethod
    def post_compose(cls, c: "Context", shared_tasks: Any) -> bool:
        """Verify deployment"""
        header(f"{cls.service} post_compose", "Verifying")
        result = shared_tasks.status(c)
        if result["is_ready"]:
            success(f"post_compose complete - {result['details']}")
            return True
        error("Verification failed", result["details"])
        return False


def make_tasks(deployer_cls: type[Deployer], shared_tasks_module: Any) -> dict:
    """
    Generate standard tasks for a deployer (DRY)
    
    Key pattern: setup checks status first, skips if healthy.
    """
    @task
    def status(c):
        """Check service status"""
        return shared_tasks_module.status(c)
    
    @task
    def pre_compose(c):
        return deployer_cls.pre_compose(c)
    
    @task
    def composing(c):
        deployer_cls.composing(c)
    
    @task
    def post_compose(c):
        return deployer_cls.post_compose(c, shared_tasks_module)
    
    @task
    def setup(c):
        """Full setup - skips if already healthy"""
        # Check if already healthy
        if deployer_cls.check_status(c, shared_tasks_module):
            success(f"{deployer_cls.service} already healthy - skipping setup")
            info("Use individual tasks (pre_compose, composing) to force reinstall")
            return
        
        # Not healthy - do full install
        warning(f"{deployer_cls.service} not healthy - starting fresh install")
        if deployer_cls.pre_compose(c) is None:
            error("pre_compose failed")
            return
        deployer_cls.composing(c)
        deployer_cls.post_compose(c, shared_tasks_module)
        success(f"{deployer_cls.service} setup complete!")
    
    return {
        "status": status,
        "pre_compose": pre_compose,
        "composing": composing,
        "post_compose": post_compose,
        "setup": setup,
    }
