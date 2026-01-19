import sys

from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("finance_report.10.app.shared")


class AppDeployer(Deployer):
    """Finance Report Application Deployer (Backend + Frontend)."""

    service = "app"
    compose_path = "finance_report/finance_report/10.app/compose.yaml"
    data_path = None  # Stateless application
    secret_key = "DATABASE_URL"
    project = "finance_report"  # Dokploy project name

    # Domain configured via compose labels, not Dokploy
    subdomain = None
    service_port = 3000
    service_name = "frontend"

    @classmethod
    def pre_compose(cls, c) -> dict | None:
        """Inject public S3 endpoint based on internal domain."""
        env_vars = super().pre_compose(c)
        if env_vars is None:
            return None
            
        # Auto-configure S3 Public Endpoint
        # Assumes Platform MinIO is exposed at s3.{INTERNAL_DOMAIN} via Traefik
        domain = env_vars.get("INTERNAL_DOMAIN")
        if domain:
            env_vars["S3_PUBLIC_ENDPOINT"] = f"https://s3.{domain}"
            
        return env_vars


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
