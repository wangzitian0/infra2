import sys

from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, SecretsFacet

shared_tasks = sys.modules.get("finance_report.01.postgres.shared")


class PostgresDeployer(Deployer):
    """Finance Report PostgreSQL Deployer."""

    service = "postgres"
    compose_path = "finance_report/finance_report/01.postgres/compose.yaml"
    data_path = "/data/finance_report/postgres"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="pg_dump_plus_data_archive",
            restore_command="restore latest finance_report pg_dump, then reattach DATA_PATH if needed.",
        ),
    )
    secret_key = "POSTGRES_PASSWORD"
    project = "finance_report"  # Dokploy project name

    # PostgreSQL Alpine requires uid=70 and chmod=700
    uid = "70"
    chmod = "700"

    # No public domain (internal only)
    subdomain = None
    service_port = 5432
    service_name = "postgres"

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth per #257/#259).
    secrets = (
        SecretsFacet(
            vault_agent_container="finance_report-postgres-vault-agent${ENV_SUFFIX}",
            app_containers=("finance_report-postgres${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )


if shared_tasks:
    _tasks = make_tasks(PostgresDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
