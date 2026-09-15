import sys

from libs.deploy.deployer import Deployer, make_tasks
from libs.service_facets import BackupFacet, ProbeFacet, SecretsFacet, SignalFacet

shared_tasks = sys.modules.get("finance_report.02.redis.shared")


class RedisDeployer(Deployer):
    """Finance Report Redis Deployer."""

    service = "redis"
    compose_path = "finance_report/finance_report/02.redis/compose.yaml"
    data_path = "/data/finance_report/redis"

    # Backup facts (#542): the backup inventory derives from these
    # (formerly the ops.backup-inventory YAML, deleted).
    backups = (
        BackupFacet(
            method="redis_rdb_archive",
            restore_command="stop Redis, restore dump.rdb, then start Redis.",
        ),
    )
    secret_key = "PASSWORD"
    project = "finance_report"  # Dokploy project name

    # No public domain (internal only)
    subdomain = None
    service_port = 6379

    # Minute-tier liveness from the probe runner (TCP: the app's own credentials are
    # not the runner's, so a protocol probe would only prove the password).
    probes = (
        ProbeFacet(
            name="finance-report-redis-tcp",
            kind="tcp",
            target="finance_report-redis${ENV_SUFFIX}:6379",
            expected="connected",
        ),
    )
    signals = (
        SignalFacet(
            tier="minute",
            type="alert",
            consecutive_failures=3,
            renotify_window_sec=1800,
        ),
    )
    service_name = "redis"

    # Vault self-refresh facts (#542): the audit inventory derives from this
    # (AppRole auth per #257/#259).
    secrets = (
        SecretsFacet(
            vault_agent_container="finance_report-redis-vault-agent${ENV_SUFFIX}",
            app_containers=("finance_report-redis${ENV_SUFFIX}",),
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
