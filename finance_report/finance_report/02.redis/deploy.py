import sys

from libs.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("finance_report.02.redis.shared")


class RedisDeployer(Deployer):
    """Finance Report Redis Deployer."""

    service = "redis"
    compose_path = "finance_report/finance_report/02.redis/compose.yaml"
    data_path = "/data/finance_report/redis"
    secret_key = "PASSWORD"
    project = "finance_report"  # Dokploy project name

    # No public domain (internal only)
    subdomain = None
    service_port = 6379
    service_name = "redis"


if shared_tasks:
    _tasks = make_tasks(RedisDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
