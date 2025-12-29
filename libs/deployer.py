"""
Base deployer with DRY task generation

Simplified: minimal class attributes, uses new env.py API.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
from invoke import task

from libs.common import get_env, validate_env
from libs.console import header, success, error, warning, info, env_vars, prompt_action, run_with_status
from libs.env import generate_password, get_secrets

if TYPE_CHECKING:
    from invoke import Context


__all__ = ['Deployer', 'make_tasks']


class Deployer:
    """Base class for service deployment.
    
    Subclass and set: service, compose_path, data_path
    Optional: secret_key (name in Vault), env_var_name (display name)
    """
    
    # Required
    service: str = ""
    compose_path: str = ""
    data_path: str = ""
    
    # Optional with defaults
    uid: str = "999"
    gid: str = "999"
    chmod: str = "755"  # Override to "700" for sensitive services like PostgreSQL
    secret_key: str = "password"
    env_var_name: str = ""
    
    @classmethod
    def env(cls) -> dict[str, str | None]:
        return get_env()
    
    @classmethod
    def secrets(cls):
        """Get secrets backend for this service"""
        e = cls.env()
        return get_secrets(
            project=e.get('PROJECT', 'platform'),
            service=cls.service,
            env=e.get('ENV', 'production')
        )
    
    @classmethod
    def _prepare_dirs(cls, c: "Context") -> bool:
        """Create data directories on VPS"""
        if missing := validate_env():
            error(f"Missing: {', '.join(missing)}")
            return False
        
        e = cls.env()
        header(f"{cls.service} pre_compose", f"Preparing ({e['ENV']})")
        
        host = e['VPS_HOST']
        run_with_status(c, f"ssh root@{host} 'mkdir -p {cls.data_path}'", "Create directory")
        run_with_status(c, f"ssh root@{host} 'chown -R {cls.uid}:{cls.gid} {cls.data_path}'", "Set ownership")
        run_with_status(c, f"ssh root@{host} 'chmod -R {cls.chmod} {cls.data_path}'", "Set permissions")
        return True
    
    @classmethod
    def pre_compose(cls, c: "Context") -> dict | None:
        """Prepare and generate secrets"""
        if not cls._prepare_dirs(c):
            return None
        
        secrets_backend = cls.secrets()
        result = {}
        
        # Get or generate primary secret
        if cls.env_var_name:
            val = secrets_backend.get(cls.secret_key)
            if not val:
                val = generate_password(24)
                secrets_backend.set(cls.secret_key, val)
            result[cls.env_var_name] = val
        
        env_vars("DOKPLOY ENV", result)
        success("pre_compose complete")
        return result
    
    @classmethod
    def composing(cls, c: "Context") -> None:
        """Guide manual Dokploy deployment"""
        e = cls.env()
        header(f"{cls.service} composing", f"Deploy {cls.service}")
        prompt_action("Deploy in Dokploy", [
            f"Access: https://cloud.{e['INTERNAL_DOMAIN']}",
            f"Compose: {cls.compose_path}",
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


def make_tasks(deployer_cls: type[Deployer], shared_tasks: Any) -> dict:
    """Generate standard invoke tasks for a deployer"""
    
    @task
    def status(c):
        """Check service status"""
        return shared_tasks.status(c)
    
    @task
    def pre_compose(c):
        return deployer_cls.pre_compose(c)
    
    @task
    def composing(c):
        deployer_cls.composing(c)
    
    @task
    def post_compose(c):
        return deployer_cls.post_compose(c, shared_tasks)
    
    @task
    def setup(c):
        """Full setup - skips if healthy"""
        try:
            result = shared_tasks.status(c)
            if result.get("is_ready"):
                success(f"{deployer_cls.service} already healthy - skipping")
                return
        except Exception as exc:
            warning(f"Status check failed: {exc}")
        
        warning(f"{deployer_cls.service} not healthy - starting install")
        if deployer_cls.pre_compose(c) is None:
            error("pre_compose failed")
            return
        deployer_cls.composing(c)
        deployer_cls.post_compose(c, shared_tasks)
        success(f"{deployer_cls.service} setup complete!")
    
    return {
        "status": status,
        "pre_compose": pre_compose,
        "composing": composing,
        "post_compose": post_compose,
        "setup": setup,
    }
