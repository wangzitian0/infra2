"""
IaC Runner Deployment

GitOps webhook service for automatic infrastructure sync.
"""
import sys
from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("bootstrap.06.iac-runner.shared")


class IaCRunnerDeployer(Deployer):
    """Deployer for IaC Runner service."""
    
    service = "iac_runner"  # Use underscore for Vault path compatibility
    compose_path = "bootstrap/06.iac-runner/compose.yaml"
    data_path = "/data/bootstrap/iac-runner"
    project = "bootstrap"
    
    # Webhook secret
    secret_key = "WEBHOOK_SECRET"
    
    # Domain config
    subdomain = "iac"
    service_port = 8080
    service_name = "iac-runner"  # Docker service name keeps hyphen


if shared_tasks:
    _tasks = make_tasks(IaCRunnerDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
