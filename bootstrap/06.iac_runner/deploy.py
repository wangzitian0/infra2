"""
IaC Runner Deployment

GitOps webhook service for automatic infrastructure sync.
"""

import sys
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import SecretsFacet

shared_tasks = sys.modules.get("bootstrap.06.iac_runner.shared")


class IaCRunnerDeployer(Deployer):
    """Deployer for IaC Runner service."""

    service = "iac_runner"  # Use underscore for Vault path compatibility
    compose_path = "bootstrap/06.iac_runner/compose.yaml"
    data_path = "/data/bootstrap/iac-runner"
    project = "bootstrap"

    # Webhook secret
    secret_key = "WEBHOOK_SECRET"

    # Simple public route owned by Dokploy Domains.
    subdomain = "iac"
    service_port = 8080
    service_name = "iac-runner"  # Docker service name keeps hyphen

    # Vault self-refresh facts (#542). The bootstrap plane is outside the
    # registry deploy fan-out, but this deploy.py is still the single
    # declaration point for iac_runner's facets — read by
    # libs.service_registry.bootstrap_facet_attrs() via the same fail-closed
    # AST reader. AppRole auth (#369, completing #257/#259): vault-agent uses
    # VAULT_ROLE_ID + VAULT_SECRET_ID (Dokploy-injected, NOT 1Password); P0
    # anti-cycle invariants preserved — see docs/ssot/bootstrap.iac_runner.md
    # §6.4. Bootstrap-plane containers carry no ${ENV_SUFFIX} (single shared
    # instance).
    secrets = (
        SecretsFacet(
            vault_agent_container="iac-runner-vault-agent",
            app_containers=("iac-runner",),
            auth_method="approle",
        ),
    )


if shared_tasks:
    _tasks = make_tasks(IaCRunnerDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
