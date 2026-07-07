import sys

from libs.deploy.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("truealpha.10.app.shared")


class AppDeployer(Deployer):
    """TrueAlpha Application Deployer (Next.js web + FastAPI llm-service)."""

    service = "app"
    compose_path = "truealpha/truealpha/10.app/compose.yaml"
    data_path = None
    secret_key = "DATABASE_URL"
    project = "truealpha"  # Dokploy project name

    # The compose owns explicit Traefik routes (truealpha[-env].<domain>), so no
    # Dokploy-managed domain here (domain routing policy: never both).
    subdomain = None
    service_port = 3000
    service_name = "web"


if shared_tasks:
    _tasks = make_tasks(AppDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
