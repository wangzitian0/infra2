import sys

from libs.deploy.deployer import Deployer, make_tasks

shared_tasks = sys.modules.get("truealpha.01.postgres.shared")


class PostgresDeployer(Deployer):
    """TrueAlpha PostgreSQL Deployer.

    Schema DDL (raw/staging/mart/dagster + kg tables) lives in the truealpha
    app repo (db/migrations) and is applied by the app repo's tooling, not by
    this compose — see truealpha/README.md.
    """

    service = "postgres"
    compose_path = "truealpha/truealpha/01.postgres/compose.yaml"
    data_path = "/data/truealpha/postgres"
    secret_key = "POSTGRES_PASSWORD"
    project = "truealpha"  # Dokploy project name

    # PostgreSQL Alpine requires uid=70 and chmod=700
    uid = "70"
    chmod = "700"

    # No public domain (internal only)
    subdomain = None
    service_port = 5432
    service_name = "postgres"

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
        base["TA_POSTGRES_HOST_PORT"] = cls._HOST_PORTS.get(base.get("ENV", ""), "127.0.0.1:0")
        return base


if shared_tasks:
    _tasks = make_tasks(PostgresDeployer, shared_tasks)
    status = _tasks["status"]
    pre_compose = _tasks["pre_compose"]
    composing = _tasks["composing"]
    post_compose = _tasks["post_compose"]
    setup = _tasks["setup"]
    sync = _tasks["sync"]
