"""PostgreSQL deployment with vault-init"""

import sys
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, ProbeFacet, SecretsFacet

shared_tasks = sys.modules.get("platform.01.postgres.shared")


class PostgresDeployer(Deployer):
    service = "postgres"
    compose_path = "platform/01.postgres/compose.yaml"
    data_path = "/data/platform/postgres"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="pg_dump_plus_data_archive",
            restore_command="restore latest pg_dump, then reattach DATA_PATH if needed.",
        ),
    )
    uid = "70"  # Alpine postgres user
    chmod = "700"
    secret_key = "root_password"

    # Infra probes (#541): rendered into INFRA_PROBE_SPECS by platform/alerting.
    probes = (
        ProbeFacet(
            name="platform-postgres-tcp",
            kind="tcp",
            target="platform-postgres${ENV_SUFFIX}:5432",
            expected="connected",
        ),
    )

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth per #257/#259).
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-postgres-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-postgres${ENV_SUFFIX}",),
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
