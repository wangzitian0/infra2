"""Redis deployment with vault-init"""

import sys
from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, ProbeFacet, SecretsFacet

shared_tasks = sys.modules.get("platform.02.redis.shared")


class RedisDeployer(Deployer):
    service = "redis"
    compose_path = "platform/02.redis/compose.yaml"
    data_path = "/data/platform/redis"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="redis_rdb_archive",
            restore_command="stop Redis, restore dump.rdb, then start Redis.",
        ),
    )
    secret_key = "password"

    # Infra probes (#541): rendered into INFRA_PROBE_SPECS by platform/alerting.
    probes = (
        ProbeFacet(
            name="platform-redis-tcp",
            kind="tcp",
            target="platform-redis${ENV_SUFFIX}:6379",
            expected="connected",
        ),
    )

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth per #257/#259).
    secrets = (
        SecretsFacet(
            vault_agent_container="platform-redis-vault-agent${ENV_SUFFIX}",
            app_containers=("platform-redis${ENV_SUFFIX}",),
            auth_method="approle",
        ),
    )


if shared_tasks:
    _tasks = make_tasks(RedisDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
