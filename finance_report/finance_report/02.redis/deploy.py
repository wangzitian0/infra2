"""Finance Report Redis: one RedisStackDeployer declaration (#1825).

common/core/stack_preset_facts.py derives the other facts from `project`.
"""

from common.common import check_service
from common.deploy.stack_presets import RedisStackDeployer, stack_tasks


class RedisDeployer(RedisStackDeployer):
    """Finance Report Redis Deployer."""

    project = "finance_report"  # Dokploy project name


# tools/loader.py reads the module tasks: status, sync, setup and the others.
globals().update(stack_tasks(RedisDeployer, check_service))
