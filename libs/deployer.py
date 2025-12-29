"""
Base deployer class with DRY task generation

Uses libs/env.py for secret management.
Key pattern: check status first, skip if healthy.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
from invoke import task
from libs.common import get_env, validate_env, load_env_keys
from libs.console import header, success, error, warning, info, env_vars, prompt_action, run_with_status
from libs.env import EnvManager, generate_password
import sys
from pathlib import Path

if TYPE_CHECKING:
    from invoke import Context

__all__ = ['Deployer', 'make_tasks', 'load_shared_tasks']


def load_shared_tasks(caller_file: str) -> Any:
    """Load shared_tasks.py from the same directory as caller"""
    import importlib.util
    shared_path = Path(caller_file).parent / "shared_tasks.py"
    module_name = f"shared_tasks_{abs(hash(str(shared_path)))}"
    spec = importlib.util.spec_from_file_location(module_name, shared_path)
    if not spec or not spec.loader:
        raise ImportError(f"Cannot load shared tasks from {shared_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    env_example_path: str = ".env.example" # Relative to deploy.py
    
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
    def get_example_keys(cls) -> list[str]:
        """Get keys from .env.example"""
        # Resolve path relative to subclass file
        deployer_file = sys.modules[cls.__module__].__file__
        path = Path(deployer_file).parent / cls.env_example_path
        return load_env_keys(str(path))
    
    @classmethod
    def check_status(cls, c: "Context", shared_tasks: Any) -> bool:
        """Check if service is already healthy"""
        try:
            result = shared_tasks.status(c)
            return result.get("is_ready", False)
        except Exception as exc:
            warning(f"{cls.service} status check failed: {exc}")
            return False
    
    @classmethod
    def _prepare_dirs(cls, c: "Context") -> bool:
        """Create data directories"""
        if missing := validate_env():
            error(f"Missing: {', '.join(missing)}")
            return False
        
        e = cls.env()
        header(f"{cls.service} pre_compose", f"Preparing ({e['ENV']})")
        ssh_user = e.get("VPS_SSH_USER") or "root"
        results = [
            run_with_status(c, f"ssh {ssh_user}@{e['VPS_HOST']} 'mkdir -p {cls.data_path}'", "Create directory"),
            run_with_status(c, f"ssh {ssh_user}@{e['VPS_HOST']} 'chown -R {cls.uid}:{cls.gid} {cls.data_path}'", "Set ownership"),
            run_with_status(c, f"ssh {ssh_user}@{e['VPS_HOST']} 'chmod -R {cls.chmod} {cls.data_path}'", "Set permissions"),
        ]
        return all(result.ok for result in results)
    
    @classmethod
    def pre_compose(cls, c: "Context") -> dict | None:
        """Prepare and generate NEW secrets (only call if not healthy)"""
        if not cls._prepare_dirs(c):
            return None
        
        env_mgr = cls.get_env_manager()
        keys = cls.get_example_keys()
        
        # Fallback if no .env.example (for legacy)
        if not keys and cls.env_var_name:
            keys = [cls.env_var_name]
            
        secrets = {}
        for key in keys:
            if key == cls.env_var_name and cls.secret_key:
                val = env_mgr.get_secret(cls.secret_key)
                if val is None:
                    val = env_mgr.get_env(key)
                if val is None:
                    val = generate_password(24)
                    if not env_mgr.set_secret(cls.secret_key, val):
                        warning(f"Failed to store secret: {cls.secret_key}")
                if val is not None:
                    secrets[key] = val
                else:
                    warning(f"Missing env var: {key} (Checked Vault/1Password)")
                continue

            val = env_mgr.get_secret(key)
            if val is None:
                val = env_mgr.get_env(key)

            if val is not None:
                secrets[key] = val
            else:
                warning(f"Missing env var: {key} (Checked Vault/1Password)")

        env_vars("DOKPLOY ENV", secrets)
        success("pre_compose complete")
        return secrets
    
    @classmethod
    def composing(cls, c: "Context", env_keys: list[str] | None = None) -> None:
        """Deploy in Dokploy"""
        e = cls.env()
        keys = env_keys or ([cls.env_var_name] if cls.env_var_name else [])
        keys = [key for key in keys if key]
        keys_display = ", ".join(keys) if keys else "(none)"
        header(f"{cls.service} composing", f"Deploy {cls.service}")
        prompt_action("Deploy in Dokploy", [
            f"Access: https://cloud.{e['INTERNAL_DOMAIN']}",
            f"Compose: {cls.compose_path}",
            f"Add env: {keys_display}",
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
