"""
Base deployer with DRY task generation

Simplified: minimal class attributes, uses new env.py API.
"""
from __future__ import annotations
from typing import TYPE_CHECKING, Any
import os
from invoke import task

from libs.common import get_env, validate_env, service_domain
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
    project: str = "platform"  # Default project
    
    # Optional with defaults
    uid: str = "999"
    gid: str = "999"
    chmod: str = "755"  # Override to "700" for sensitive services like PostgreSQL
    secret_key: str = "password"
    env_var_name: str = ""
    
    # Domain configuration (optional)
    subdomain: str = None  # e.g., "sso" for sso.{INTERNAL_DOMAIN}
    service_port: int = None  # Container port
    service_name: str = None  # For multi-service composes

    @classmethod
    def env(cls) -> dict[str, str | None]:
        return get_env()

    @classmethod
    def project_name(cls, env: dict | None = None) -> str:
        """Get project name, prioritizing class attribute over env var.
        
        The class `project` attribute takes precedence. Only uses PROJECT env var
        if the class uses the default 'platform' project (for backward compat).
        """
        if cls.project != "platform":
            # Class explicitly sets a non-default project, use it
            return cls.project
        e = env or cls.env()
        return e.get("PROJECT") or cls.project

    @classmethod
    def data_path_for_env(cls, env: dict | None = None) -> str:
        e = env or cls.env()
        explicit_path = e.get("DATA_PATH")
        if explicit_path:
            return explicit_path
        env_name = e.get("ENV", "production")
        project = cls.project_name(e)
        if env_name == "production" or project == "bootstrap":
            return cls.data_path
        suffix = e.get("ENV_SUFFIX")
        if suffix:
            return f"{cls.data_path}{suffix}"
        if os.environ.get("ALLOW_SHARED_DATA_PATH") == "1":
            return cls.data_path
        raise ValueError(
            "Non-production requires DATA_PATH or ENV_SUFFIX to avoid data collisions. "
            "Set DATA_PATH (recommended) or ENV_SUFFIX; override with ALLOW_SHARED_DATA_PATH=1 if intentional."
        )

    @classmethod
    def compose_env_base(cls, env: dict | None = None) -> dict[str, str]:
        e = env or cls.env()
        base = {
            "ENV": e.get("ENV", "production"),
            "ENV_DOMAIN_SUFFIX": e.get("ENV_DOMAIN_SUFFIX"),
            "INTERNAL_DOMAIN": e.get("INTERNAL_DOMAIN"),
        }
        data_path = cls.data_path_for_env(e)
        if data_path:
            base["DATA_PATH"] = data_path
        if e.get("ENV_SUFFIX"):
            base["ENV_SUFFIX"] = e.get("ENV_SUFFIX")
        return {k: v for k, v in base.items() if v is not None}
    
    @classmethod
    def secrets(cls):
        """Get secrets backend for this service"""
        e = cls.env()
        # Use cls.project if PROJECT env not set
        project = cls.project_name(e)
        return get_secrets(
            project=project,
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
        try:
            data_path = cls.data_path_for_env(e)
        except ValueError as exc:
            error(str(exc))
            return False
        header(f"{cls.service} pre_compose", f"Preparing ({e['ENV']})")
        
        host = e['VPS_HOST']
        run_with_status(c, f"ssh root@{host} 'mkdir -p {data_path}'", "Create directory")
        run_with_status(c, f"ssh root@{host} 'chown -R {cls.uid}:{cls.gid} {data_path}'", "Set ownership")
        run_with_status(c, f"ssh root@{host} 'chmod -R {cls.chmod} {data_path}'", "Set permissions")
        return True
    
    @classmethod
    def pre_compose(cls, c: "Context") -> dict | None:
        """Prepare directories and ensure secrets exist in Vault.
        
        For vault-init pattern: secrets are fetched at container runtime,
        so we only ensure they exist and return VAULT_ADDR.
        """
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        secrets_backend = cls.secrets()
        
        # Get or generate primary secret
        if cls.secret_key:
            val = secrets_backend.get(cls.secret_key)
            if not val:
                val = generate_password(24)
                if secrets_backend.set(cls.secret_key, val):
                    warning(f"Generated new secret in Vault: {cls.secret_key}")
                else:
                    error(f"Failed to store secret in Vault: {cls.secret_key}")
                    return None
            else:
                info(f"Vault secret exists: {cls.secret_key}")
        
        # Return base env vars + VAULT_ADDR for vault-init pattern
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}")
        
        env_vars("DOKPLOY ENV (vault-init)", result)
        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result
    
    @classmethod
    def get_compose_content(cls, c: "Context") -> str:
        """Get compose file content. Default: read from compose_path."""
        try:
            with open(cls.compose_path, "r") as f:
                return f.read()
        except FileNotFoundError:
            error(f"Compose file not found at path: {cls.compose_path}")
            raise
        except OSError as exc:
            error(f"Failed to read compose file at '{cls.compose_path}': {exc}")
            raise
            
    @classmethod
    def composing(cls, c: "Context", env_vars: dict[str, str]) -> str:
        """Deploy via Dokploy API using GitHub provider. Returns composeId."""
        from libs.dokploy import get_dokploy, ensure_project
        from libs.const import GITHUB_OWNER, GITHUB_REPO, GITHUB_BRANCH
        
        e = cls.env()
        header(f"{cls.service} composing", f"Deploying via Dokploy API (GitHub)")
        
        # Deploy via API
        # Priority: ENV > Class Attribute > Default "platform"
        env_name = e.get("ENV", "production")
        project_name = cls.project_name(e)
        domain = e.get('INTERNAL_DOMAIN')
        host = f"cloud.{domain}" if domain else None
        
        client = get_dokploy(host=host)
        
        # Ensure project exists
        project_id, env_id = ensure_project(
            project_name,
            f"Platform services: {project_name}",
            host=host,
            env_name=env_name,
            require_env=env_name != "production",
        )
        if not env_id:
            error("Failed to get environment ID")
            raise ValueError("Failed to get environment ID")
        
        # Get GitHub provider ID
        github_id = client.get_github_provider_id()
        if not github_id:
            error("No GitHub provider configured in Dokploy. Please add one in Settings -> Git Providers.")
            raise ValueError("No GitHub provider found")
        
        info(f"Using GitHub provider: {github_id}")
        
        # Format env vars
        env_str = "\n".join(f"{k}={v}" for k, v in env_vars.items() if v is not None)
        
        # Check if compose already exists
        existing = client.find_compose_by_name(cls.service, project_name, env_name=env_name)
        
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
                env=env_str,
            )
        else:
            info("Creating new compose service with GitHub provider")
            result = client.create_compose(
                environment_id=env_id,
                name=cls.service,
                app_name=f"{project_name}-{cls.service}",
                source_type="github",
                githubId=github_id,
                repository=GITHUB_REPO,
                owner=GITHUB_OWNER,
                branch=GITHUB_BRANCH,
                composePath=cls.compose_path,
                env=env_str,
            )
            compose_id = result["composeId"]
        
        # Deploy
        info(f"Deploying compose {compose_id}...")
        client.deploy_compose(compose_id)
        
        # Configure domain if specified
        if cls.subdomain and cls.service_port:
            domain_host = service_domain(cls.subdomain, e)
            if not domain_host:
                warning("Domain configuration skipped: INTERNAL_DOMAIN missing")
            else:
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
                    # Redeploy to apply domain labels
                    info("Redeploying to apply domain labels...")
                    client.deploy_compose(compose_id)
                    success("Domain labels updated")
                elif result["skipped"] > 0:
                    info(f"Domain already configured: {domain_host}")
                if result["conflicts"]:
                    for c in result["conflicts"]:
                        warning(f"Domain conflict: {c['host']} exists with port {c['existing_port']}, need {c['desired_port']}")
        
        success(f"Deployed {cls.service} (composeId: {compose_id})")
        return compose_id
    
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
    def composing(c, env_vars=None):
        if env_vars is None:
            warning("Running composing manually - fetching secrets first")
            env_vars = deployer_cls.pre_compose(c)
        if env_vars:
            return deployer_cls.composing(c, env_vars)
        return None
    
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
        env_vars = deployer_cls.pre_compose(c)
        if env_vars is None:
            error("pre_compose failed")
            return
        deployer_cls.composing(c, env_vars)
        deployer_cls.post_compose(c, shared_tasks)
        success(f"{deployer_cls.service} setup complete!")
    
    return {
        "status": status,
        "pre_compose": pre_compose,
        "composing": composing,
        "post_compose": post_compose,
        "setup": setup,
    }
