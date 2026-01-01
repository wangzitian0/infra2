"""SigNoz deployment - observability platform"""
import sys
from libs.deployer import Deployer, make_tasks
from libs.console import success, info, warning, run_with_status
from libs.env import generate_password

shared_tasks = sys.modules.get("platform.11.signoz.shared")


class SigNozDeployer(Deployer):
    service = "signoz"
    compose_path = "platform/11.signoz/compose.yaml"
    data_path = "/data/platform/signoz"
    
    # Domain configuration (no SSO for now)
    subdomain = None  # Using Traefik labels in compose.yaml
    service_port = 3301
    service_name = "frontend"
    
    # SigNoz specific secret
    secret_key = "jwt_secret"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories and secrets for SigNoz."""
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        secrets_backend = cls.secrets()
        
        # Create data directory for query-service SQLite
        run_with_status(
            c, 
            f"ssh root@{e['VPS_HOST']} 'mkdir -p {cls.data_path}/data'",
            "Create data directory"
        )
        
        # Set permissions (SigNoz runs as root in container, but let's be explicit)
        run_with_status(
            c,
            f"ssh root@{e['VPS_HOST']} 'chmod -R 755 {cls.data_path}'",
            "Set permissions"
        )
        
        # Get or generate JWT secret from Vault
        jwt_secret = secrets_backend.get(cls.secret_key)
        if not jwt_secret:
            jwt_secret = generate_password(32)
            if secrets_backend.set(cls.secret_key, jwt_secret):
                warning(f"Generated new JWT secret in Vault: {cls.secret_key}")
            else:
                # Fallback: generate locally if Vault write fails
                warning("Failed to store JWT secret in Vault, using local generation")
        else:
            info(f"Vault secret exists: {cls.secret_key}")
        
        success("pre_compose complete")
        info(f"Frontend will be available at: https://signoz.{e.get('INTERNAL_DOMAIN', 'localhost')}")
        info("OTLP endpoints: 4317 (gRPC), 4318 (HTTP)")
        
        return {
            "INTERNAL_DOMAIN": e.get("INTERNAL_DOMAIN", "localhost"),
            "SIGNOZ_JWT_SECRET": jwt_secret,
        }


if shared_tasks:
    _tasks = make_tasks(SigNozDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
