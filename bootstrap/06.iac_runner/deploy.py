"""
IaC Runner Deployment

GitOps webhook service for automatic infrastructure sync.
"""

import sys
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, SecretsFacet

shared_tasks = sys.modules.get("bootstrap.06.iac_runner.shared")


class IaCRunnerDeployer(Deployer):
    """Deployer for IaC Runner service."""

    service = "iac_runner"  # Use underscore for Vault path compatibility
    compose_path = "bootstrap/06.iac_runner/compose.yaml"
    data_path = "/data/bootstrap/iac-runner"

    # Backup facts (#542): the backup inventory derives from these (formerly
    # the ops.backup-inventory YAML, deleted). bootstrap/1password and
    # bootstrap/vault have no deploy.py of their own, so their entries are
    # declared HERE with explicit service_id/data_path overrides — this file is
    # the bootstrap plane's single declaration point (same convention as the
    # SecretsFacet/ProbeFacet out-of-registry declarations).
    backups = (
        BackupFacet(
            method="filesystem_archive",
            restore_command="restore IaC Runner workspace cache; service can reclone if missing.",
        ),
        BackupFacet(
            service_id="bootstrap/1password",
            data_path="/data/bootstrap/1password",
            method="encrypted_filesystem_archive",
            restore_command="restore 1Password Connect data directory from the selected off-host archive.",
        ),
        BackupFacet(
            service_id="bootstrap/vault",
            data_path="/data/bootstrap/vault",
            method="vault_file_storage_archive",
            restore_command="restore Vault file storage, then unseal with operator-held recovery material.",
        ),
    )
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
