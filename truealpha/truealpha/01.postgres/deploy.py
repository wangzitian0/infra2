"""TrueAlpha PostgreSQL: one PostgresStackDeployer declaration (#1825).

common/core/stack_preset_facts.py derives the other facts from `project`.
"""

from common.common import check_service
from common.deploy.stack_presets import PostgresStackDeployer, stack_tasks


class PostgresDeployer(PostgresStackDeployer):
    """TrueAlpha PostgreSQL Deployer.

    Schema DDL (raw/staging/mart/dagster + kg tables) lives in the truealpha
    app repo (db/migrations) and is applied by the app repo's tooling, not by
    this compose — see truealpha/README.md.
    """

    project = "truealpha"  # Dokploy project name

    # Host-loopback port per env (the ingestion runtime runs as HOST processes
    # next to moomoo OpenD, and cannot route into the dokploy overlay). Fixed
    # ports for the long-lived envs so host .env files stay stable; anything
    # else (preview lanes) gets an ephemeral port.
    _HOST_PORTS = {"staging": "127.0.0.1:15432", "production": "127.0.0.1:15433"}

    @classmethod
    def compose_env_base(cls, env: dict | None = None) -> dict[str, str]:
        # Injected here, NOT in pre_compose: the iac-runner's sync path builds
        # the Dokploy env straight from compose_env_base ("without full
        # pre_compose side effects"), so a pre_compose override never reaches a
        # real deploy — the v1.1.24 rollout shipped an ephemeral port because
        # of exactly that. compose_env_base flows through BOTH sync and the
        # manual pre_compose/setup tasks.
        base = super().compose_env_base(env)
        base["TA_POSTGRES_HOST_PORT"] = cls._HOST_PORTS.get(
            base.get("ENV", ""), "127.0.0.1:0"
        )
        return base


# tools/loader.py reads the module tasks: status, sync, setup and the others.
globals().update(stack_tasks(PostgresDeployer, check_service))
