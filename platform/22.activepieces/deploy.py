"""Activepieces deployment with vault-init"""
import sys
import os
from libs.deployer import Deployer, make_tasks
from libs.common import with_env_suffix
from libs.console import success, warning, info, run_with_status, header
from libs.env import generate_password, get_secrets

shared_tasks = sys.modules.get("platform.22.activepieces.shared")


class ActivepiecesDeployer(Deployer):
    service = "activepieces"
    compose_path = "platform/22.activepieces/compose.yaml"
    data_path = "/data/platform/activepieces"
    uid = "1000"
    gid = "1000"
    secret_key = "encryption_key"
    
    # Domain configuration - None to use compose.yaml Traefik labels (SSO protected)
    subdomain = None
    service_port = 80
    service_name = "activepieces"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories, check dependencies, ensure secrets exist in Vault."""
        from libs.console import fatal
        
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        env_name = e.get('ENV', 'production')
        project = e.get('PROJECT', 'platform')
        internal_domain = e.get('INTERNAL_DOMAIN')
        env_domain_suffix = e.get('ENV_DOMAIN_SUFFIX', '')
        
        # Check Vault access
        if not os.getenv('VAULT_ROOT_TOKEN'):
            fatal(
                "VAULT_ROOT_TOKEN not set",
                "Required for: 1) Reading postgres/redis passwords, 2) Storing activepieces secrets\n"
                "   Get token: op read 'op://Infra2/dexluuvzg5paff3cltmtnlnosm/Root Token' "
                "(or /Token; item: bootstrap/vault/Root Token)\n"
                "   Then: export VAULT_ROOT_TOKEN=<token>"
            )
        
        # Check dependencies exist in Vault
        pg_secrets = get_secrets(project, "postgres", env_name)
        redis_secrets = get_secrets(project, "redis", env_name)
        
        if not pg_secrets.get("root_password"):
            fatal("Postgres password not found in Vault", "Run: export VAULT_ROOT_TOKEN=<token> && invoke postgres.setup")
        if not redis_secrets.get("password"):
            fatal("Redis password not found in Vault", "Run: export VAULT_ROOT_TOKEN=<token> && invoke redis.setup")
        success("Verified postgres and redis secrets in Vault")
        
        # Create database (idempotent)
        postgres_container = with_env_suffix("platform-postgres", e)
        result = c.run(
            f"ssh root@{e['VPS_HOST']} \"docker exec {postgres_container} psql -U postgres -c 'CREATE DATABASE activepieces;'\"",
            warn=True,
            hide=True,
        )
        if result.failed:
            stderr = result.stderr or ""
            if "already exists" in stderr:
                info("Database 'activepieces' already exists")
            else:
                fatal(
                    "Failed to create 'activepieces' database",
                    f"Error: {stderr}"
                )
        else:
            success("Database created")
        
        # Ensure Activepieces secrets exist
        activepieces_secrets = get_secrets(project, "activepieces", env_name)
        
        # Encryption key (32 hex characters)
        encryption_key = activepieces_secrets.get("encryption_key")
        if not encryption_key:
            import secrets as py_secrets
            encryption_key = py_secrets.token_hex(16)  # 32 hex chars
            if not activepieces_secrets.set("encryption_key", encryption_key):
                fatal("Failed to store encryption key in Vault")
            warning("Generated new encryption key in Vault")
        else:
            info("Encryption key exists in Vault")
        
        # JWT secret
        jwt_secret = activepieces_secrets.get("jwt_secret")
        if not jwt_secret:
            jwt_secret = generate_password(32)
            if not activepieces_secrets.set("jwt_secret", jwt_secret):
                fatal("Failed to store JWT secret in Vault")
            warning("Generated new JWT secret in Vault")
        else:
            info("JWT secret exists in Vault")
        
        # Frontend URL
        frontend_url = activepieces_secrets.get("frontend_url")
        if not frontend_url:
            frontend_url = f"https://automate{env_domain_suffix}.{internal_domain}"
            if not activepieces_secrets.set("frontend_url", frontend_url):
                fatal("Failed to store frontend URL in Vault")
            warning(f"Set frontend URL: {frontend_url}")
        else:
            info(f"Frontend URL exists: {frontend_url}")
        
        # Return VAULT_ADDR for vault-init pattern
        result = cls.compose_env_base(e)
        result["VAULT_ADDR"] = e.get("VAULT_ADDR", f"https://vault.{e.get('INTERNAL_DOMAIN', 'localhost')}")
        
        success("pre_compose complete - vault-init will fetch secrets at runtime")
        info("\nNote: VAULT_APP_TOKEN auto-configured via 'invoke vault.setup-tokens'")
        return result
    
    @classmethod
    def post_compose(cls, c, shared_tasks):
        """Verify deployment and setup SSO protection"""
        from libs.console import header, success, error, info
        
        header(f"{cls.service} post_compose", "Verifying")
        result = shared_tasks.status(c)
        if result["is_ready"]:
            success(f"post_compose complete - {result['details']}")
            
            # Remind about SSO setup
            e = cls.env()
            env_domain_suffix = e.get('ENV_DOMAIN_SUFFIX', '')
            internal_domain = e.get('INTERNAL_DOMAIN')
            
            info("\n📌 Next: Configure SSO protection in Authentik")
            info(f"   invoke authentik.shared.create-proxy-app \\")
            info(f"     --name=Activepieces \\")
            info(f"     --slug=activepieces \\")
            info(f"     --external-host=https://automate{env_domain_suffix}.{internal_domain} \\")
            info(f"     --internal-host=platform-activepieces{e.get('ENV_SUFFIX', '')} \\")
            info(f"     --port=80")
            return True
        error("Verification failed", result["details"])
        return False


if shared_tasks:
    _tasks = make_tasks(ActivepiecesDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
