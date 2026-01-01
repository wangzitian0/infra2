"""SigNoz deployment - observability platform"""
import sys
from libs.deployer import Deployer, make_tasks
from libs.console import success, info, run_with_status

shared_tasks = sys.modules.get("platform.11.signoz.shared")


class SigNozDeployer(Deployer):
    service = "signoz"
    compose_path = "platform/11.signoz/compose.yaml"
    data_path = "/data/platform/signoz"
    
    # Domain configuration (no SSO for now)
    subdomain = None  # Using Traefik labels in compose.yaml
    service_port = 3301
    service_name = "frontend"

    @classmethod
    def pre_compose(cls, c):
        """Prepare directories for SigNoz data."""
        if not cls._prepare_dirs(c):
            return None
        
        e = cls.env()
        
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
        
        success("pre_compose complete")
        info(f"Frontend will be available at: https://signoz.{e.get('INTERNAL_DOMAIN', 'localhost')}")
        info("OTLP endpoints: 4317 (gRPC), 4318 (HTTP)")
        
        return {
            "INTERNAL_DOMAIN": e.get("INTERNAL_DOMAIN", "localhost"),
        }


if shared_tasks:
    _tasks = make_tasks(SigNozDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
