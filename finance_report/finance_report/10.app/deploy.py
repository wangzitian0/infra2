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


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
